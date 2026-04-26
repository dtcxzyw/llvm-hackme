from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.models import (
    BugKind,
    PullRequest,
    PullRequestUpdate,
    Reproducer,
    ReviewDecision,
)
from llvm_hackme.service import HackmeService


async def test_reverify_existing_reproducer_still_reproduces_skips_fuzz() -> None:
    pr = PullRequest(
        number=1,
        title="T",
        author_login="user",
        head_sha="sha",
        updated_at=MagicMock(),
        html_url="h",
    )
    update = PullRequestUpdate(pr=pr, patch="patch", patch_sha256="p2")

    stored_reproducer = Reproducer(
        kind=BugKind.CRASH,
        source_path=MagicMock(),
        command=["opt", "test.ll"],
        baseline_revision="oldrev",
        pr_head_sha="oldsha",
        patch_sha256="oldpatch",
        stacktrace="crash detail",
    )

    state_mock = MagicMock()
    state_mock.get_pull_state.return_value = MagicMock(
        pr_number=1,
        head_sha="oldsha",
        patch_sha256="oldpatch",
        comment_id=5,
        comment_url="https://comment",
        reproducer=stored_reproducer,
    )

    reviewer_mock = MagicMock(spec=OpenAIPatchReviewer)
    reviewer_mock.review = AsyncMock(
        return_value=ReviewDecision(accepted=True, reason="")
    )

    builds_mock = MagicMock()
    toolchain = MagicMock()
    toolchain.baseline_revision = "rev"
    builds_mock.prepare_pr_build = AsyncMock(return_value=toolchain)

    fuzzer_mock = MagicMock()
    fuzzer_mock.run = AsyncMock()

    with (
        patch(
            "llvm_hackme.service.verify_reproducer", new_callable=AsyncMock
        ) as mock_verify,
        patch(
            "llvm_hackme.service.report_result", new_callable=AsyncMock
        ) as mock_report,
    ):
        mock_verify.return_value = stored_reproducer

        service = HackmeService.__new__(HackmeService)
        service._config = MagicMock()
        service._config.debounce_seconds = 0
        service._state = state_mock
        service._github = MagicMock(spec=GitHubClient)
        service._reviewer = reviewer_mock
        service._builds = builds_mock
        service._fuzzer = fuzzer_mock
        service._build_lock = MagicMock()
        service._build_lock.__aenter__ = AsyncMock()
        service._build_lock.__aexit__ = AsyncMock()
        service._pr_tasks = {}
        service._service_login = "service-login"
        service._status_callback = None

        await service._handle_pr_update(update)

        mock_verify.assert_called_once_with(stored_reproducer, toolchain)
        mock_report.assert_called_once_with(
            service._github,
            state_mock,
            update,
            stored_reproducer,
            toolchain.baseline_revision,
            "service-login",
        )
        fuzzer_mock.run.assert_not_called()


async def test_reverify_existing_reproducer_gone_proceeds_to_fuzz() -> None:
    pr = PullRequest(
        number=1,
        title="T",
        author_login="user",
        head_sha="sha",
        updated_at=MagicMock(),
        html_url="h",
    )
    update = PullRequestUpdate(pr=pr, patch="patch", patch_sha256="p2")

    stored_reproducer = Reproducer(
        kind=BugKind.CRASH,
        source_path=MagicMock(),
        command=["opt", "test.ll"],
        baseline_revision="oldrev",
        pr_head_sha="oldsha",
        patch_sha256="oldpatch",
        stacktrace="crash detail",
    )

    state_mock = MagicMock()
    state_mock.get_pull_state.return_value = MagicMock(
        pr_number=1,
        head_sha="oldsha",
        patch_sha256="oldpatch",
        comment_id=5,
        comment_url="https://comment",
        reproducer=stored_reproducer,
    )

    reviewer_mock = MagicMock(spec=OpenAIPatchReviewer)
    reviewer_mock.review = AsyncMock(
        return_value=ReviewDecision(accepted=True, reason="")
    )

    builds_mock = MagicMock()
    toolchain = MagicMock()
    toolchain.baseline_revision = "rev"
    builds_mock.prepare_pr_build = AsyncMock(return_value=toolchain)

    fuzz_mock = AsyncMock()
    fuzz_mock.return_value = MagicMock(reproducer=None)
    fuzzer_mock = MagicMock()
    fuzzer_mock.run = fuzz_mock

    with (
        patch(
            "llvm_hackme.service.verify_reproducer", new_callable=AsyncMock
        ) as mock_verify,
        patch(
            "llvm_hackme.service.report_result", new_callable=AsyncMock
        ) as mock_report,
    ):
        mock_verify.return_value = None

        service = HackmeService.__new__(HackmeService)
        service._config = MagicMock()
        service._config.debounce_seconds = 0
        service._state = state_mock
        service._github = MagicMock(spec=GitHubClient)
        service._reviewer = reviewer_mock
        service._builds = builds_mock
        service._fuzzer = fuzzer_mock
        service._build_lock = MagicMock()
        service._build_lock.__aenter__ = AsyncMock()
        service._build_lock.__aexit__ = AsyncMock()
        service._pr_tasks = {}
        service._service_login = "service-login"
        service._status_callback = None

        await service._handle_pr_update(update)

        mock_verify.assert_called_once_with(stored_reproducer, toolchain)
        fuzzer_mock.run.assert_called_once()
        mock_report.assert_called_once()
