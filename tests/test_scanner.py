from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from llvm_hackme.github import GitHubClient
from llvm_hackme.models import PullRequest
from llvm_hackme.scanner import PullRequestScanner
from llvm_hackme.state import StateStore


@pytest.fixture
def mock_github() -> MagicMock:
    return MagicMock(spec=GitHubClient)


@pytest.fixture
def mock_state(tmp_path) -> MagicMock:
    path = tmp_path / "test.db"
    store = StateStore(path)
    return store


class TestScanner:
    @pytest.mark.asyncio
    async def test_scan_no_watermark(
        self, mock_github: MagicMock, mock_state: StateStore
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_pr = PullRequest(
            number=1,
            title="Test",
            author_login="user",
            head_sha="sha1",
            updated_at=now,
            html_url="https://example.com",
        )
        mock_github.list_recent_open_pull_requests = AsyncMock(
            return_value=([mock_pr], now)
        )
        mock_github.list_pull_files = AsyncMock(
            return_value=["llvm/lib/Transforms/InstCombine/foo.cpp"]
        )
        mock_github.get_pull_patch = AsyncMock(return_value="diff --git a/test\n+foo\n")

        scanner = PullRequestScanner(
            MagicMock(scan_overlap_seconds=300),
            mock_state,
            mock_github,
        )
        updates = await scanner.scan_once()
        assert len(updates) == 1
        assert updates[0].pr.number == 1
        assert updates[0].patch == "diff --git a/test\n+foo\n"

        stored = mock_state.get_pull_state(1)
        assert stored.head_sha == "sha1"
        assert stored.patch_sha256 is not None
        assert len(stored.patch_sha256) == 64

    @pytest.mark.asyncio
    async def test_scan_filters_non_relevant(
        self, mock_github: MagicMock, mock_state: StateStore
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_pr = PullRequest(
            number=2,
            title="Not relevant",
            author_login="user",
            head_sha="sha2",
            updated_at=now,
            html_url="https://example.com",
        )
        mock_github.list_recent_open_pull_requests = AsyncMock(
            return_value=([mock_pr], now)
        )
        mock_github.list_pull_files = AsyncMock(
            return_value=["clang/lib/Sema/SemaExpr.cpp"]
        )

        scanner = PullRequestScanner(
            MagicMock(scan_overlap_seconds=300),
            mock_state,
            mock_github,
        )
        updates = await scanner.scan_once()
        assert len(updates) == 0

    @pytest.mark.asyncio
    async def test_scan_filters_test_only(
        self, mock_github: MagicMock, mock_state: StateStore
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_pr = PullRequest(
            number=5,
            title="Test file only",
            author_login="user",
            head_sha="sha5",
            updated_at=now,
            html_url="https://example.com",
        )
        mock_github.list_recent_open_pull_requests = AsyncMock(
            return_value=([mock_pr], now)
        )
        mock_github.list_pull_files = AsyncMock(
            return_value=["llvm/test/Transforms/InstCombine/add.ll"]
        )

        scanner = PullRequestScanner(
            MagicMock(scan_overlap_seconds=300),
            mock_state,
            mock_github,
        )
        updates = await scanner.scan_once()
        assert len(updates) == 0

    @pytest.mark.asyncio
    async def test_scan_skips_already_processed(
        self, mock_github: MagicMock, mock_state: StateStore
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_pr = PullRequest(
            number=3,
            title="Already seen",
            author_login="user",
            head_sha="sha3",
            updated_at=now,
            html_url="https://example.com",
        )
        mock_github.list_recent_open_pull_requests = AsyncMock(
            return_value=([mock_pr], now)
        )
        mock_github.list_pull_files = AsyncMock(
            return_value=["llvm/lib/Transforms/InstCombine/x.cpp"]
        )
        mock_github.get_pull_patch = AsyncMock(return_value="patch-body")

        import hashlib

        patch_sha = hashlib.sha256(b"patch-body").hexdigest()
        mock_state.record_pr_update(3, head_sha="sha3", patch_sha256=patch_sha)
        mock_state.mark_processed(3)

        scanner = PullRequestScanner(
            MagicMock(scan_overlap_seconds=300),
            mock_state,
            mock_github,
        )
        updates = await scanner.scan_once()
        assert len(updates) == 0

    @pytest.mark.asyncio
    async def test_scan_resumes_unprocessed(
        self, mock_github: MagicMock, mock_state: StateStore
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_pr = PullRequest(
            number=4,
            title="Unprocessed",
            author_login="user",
            head_sha="sha4",
            updated_at=now,
            html_url="https://example.com",
        )
        mock_github.list_recent_open_pull_requests = AsyncMock(
            return_value=([mock_pr], now)
        )
        mock_github.list_pull_files = AsyncMock(
            return_value=["llvm/lib/Transforms/InstCombine/x.cpp"]
        )
        mock_github.get_pull_patch = AsyncMock(return_value="patch-body")

        import hashlib

        patch_sha = hashlib.sha256(b"patch-body").hexdigest()
        mock_state.record_pr_update(4, head_sha="sha4", patch_sha256=patch_sha)

        scanner = PullRequestScanner(
            MagicMock(scan_overlap_seconds=300),
            mock_state,
            mock_github,
        )
        updates = await scanner.scan_once()
        assert len(updates) == 1
