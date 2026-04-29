from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llvm_hackme.github import GitHubClient, IssueComment
from llvm_hackme.models import (
    BugKind,
    CommentState,
    PullRequest,
    PullRequestUpdate,
    Reproducer,
)
from llvm_hackme.reporting import (
    COMMENT_FIRST_LINE,
    find_llvm_hackme_comment,
    make_comment_body,
    report_result,
)


class TestCommentBody:
    def test_bug_found_crash(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
            stacktrace="SIGSEGV at address 0x0",
        )
        body = make_comment_body(
            CommentState.BUG_FOUND,
            reproducer,
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
        )
        assert "The following correctness issue was found by" in body
        assert "llvm-hackme" in body
        assert "bug_found" in body
        assert "rev123" in body
        assert "sha456" in body
        assert "sha256abc" in body
        assert "crash" in body.lower()
        assert "SIGSEGV" in body

    def test_bug_found_miscompilation(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=MagicMock(),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
            alive2_counterexample="Transformation doesn't verify!\nERROR: ...",
        )
        body = make_comment_body(
            CommentState.BUG_FOUND,
            reproducer,
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
        )
        assert "bug_found" in body
        assert "miscompilation" in body
        assert "Transformation doesn't verify!" in body

    def test_still_reproduces(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="rev123",
            pr_head_sha="newsha",
            patch_sha256="newsha256",
            stacktrace="crash",
        )
        body = make_comment_body(
            CommentState.STILL_REPRODUCES,
            reproducer,
            baseline_revision="rev123",
            pr_head_sha="newsha",
            patch_sha256="newsha256",
        )
        assert "still_reproduces" in body
        assert "still reproduces" in body
        assert "**Baseline Revision**: `rev123`" in body
        assert "**PR Head SHA**: `newsha`" in body
        assert "**Patch SHA256**: `newsha256`" in body

    def test_no_issue_found(self) -> None:
        body = make_comment_body(
            CommentState.NO_ISSUE_FOUND_FOR_CURRENT_PATCH,
            None,
            baseline_revision="baserev",
            pr_head_sha="headsha",
            patch_sha256="patchsha256",
        )
        assert "no_issue_found" in body
        assert "did not identify" in body
        assert "**Baseline Revision**: `baserev`" in body
        assert "**PR Head SHA**: `headsha`" in body
        assert "**Patch SHA256**: `patchsha256`" in body

    def test_first_line_is_exact(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="s",
            patch_sha256="p",
            stacktrace="st",
        )
        for state in CommentState:
            body = make_comment_body(
                state,
                reproducer,
                baseline_revision="r",
                pr_head_sha="s",
                patch_sha256="p",
            )
            first_line = body.strip().split("\n", 1)[0].strip()
            assert first_line == COMMENT_FIRST_LINE


class TestFindComment:
    def test_finds_matching_comment(self) -> None:
        comments = [
            IssueComment(
                id=1, html_url="url1", body="Other comment", author_login="other"
            ),
            IssueComment(
                id=2,
                html_url="url2",
                body=f"{COMMENT_FIRST_LINE}\nRest of body",
                author_login="service-login",
            ),
        ]
        result = find_llvm_hackme_comment(comments, "service-login")
        assert result is not None
        assert result.id == 2

    def test_skips_non_matching_first_line(self) -> None:
        comments = [
            IssueComment(
                id=3,
                html_url="url3",
                body=(
                    "Different first line\nThe following correctness "
                    "issue was found by llvm-hackme."
                ),
                author_login="service-login",
            ),
        ]
        result = find_llvm_hackme_comment(comments, "service-login")
        assert result is None

    def test_skips_wrong_author(self) -> None:
        comments = [
            IssueComment(
                id=4,
                html_url="url4",
                body=COMMENT_FIRST_LINE,
                author_login="wrong-login",
            ),
        ]
        result = find_llvm_hackme_comment(comments, "service-login")
        assert result is None

    def test_empty_list(self) -> None:
        result = find_llvm_hackme_comment([], "service-login")
        assert result is None


class TestReportResult:
    @pytest.mark.asyncio
    async def test_no_reproducer_no_comment(self) -> None:
        github = MagicMock(spec=GitHubClient)
        github.list_issue_comments = AsyncMock(return_value=[])
        state = MagicMock()
        state.get_pull_state.return_value = MagicMock(
            pr_number=1,
            head_sha="s",
            patch_sha256="p",
            comment_id=None,
            comment_url=None,
            reproducer=None,
        )

        pr = PullRequest(
            number=1,
            title="T",
            author_login="user",
            head_sha="s",
            updated_at=MagicMock(),
            html_url="h",
        )
        update = PullRequestUpdate(pr=pr, patch="p", patch_sha256="p")

        await report_result(github, state, update, None, "rev123", "service-login")

        github.create_issue_comment.assert_not_called()
        github.update_issue_comment.assert_not_called()
        github.create_request_changes_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_bug_found_creates_comment_and_review(self) -> None:
        github = MagicMock(spec=GitHubClient)
        github.list_issue_comments = AsyncMock(return_value=[])
        created_comment = IssueComment(
            id=10, html_url="https://comment/url", body="body", author_login="svc"
        )
        github.create_issue_comment = AsyncMock(return_value=created_comment)

        state = MagicMock()
        state.get_pull_state.return_value = MagicMock(
            pr_number=1,
            head_sha="s",
            patch_sha256="p",
            comment_id=None,
            comment_url=None,
            reproducer=None,
        )

        pr = PullRequest(
            number=1,
            title="T",
            author_login="other-user",
            head_sha="s",
            updated_at=MagicMock(),
            html_url="h",
        )
        update = PullRequestUpdate(pr=pr, patch="p", patch_sha256="p")
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="s",
            patch_sha256="p",
            stacktrace="crash",
        )

        await report_result(
            github, state, update, reproducer, "rev123", "service-login"
        )

        github.create_issue_comment.assert_called_once()
        github.create_request_changes_review.assert_called_once()
        state.save_comment.assert_called_once()
        state.save_reproducer.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_review_for_service_author(self) -> None:
        github = MagicMock(spec=GitHubClient)
        github.list_issue_comments = AsyncMock(return_value=[])
        created = IssueComment(
            id=10, html_url="https://comment/url", body="body", author_login="svc"
        )
        github.create_issue_comment = AsyncMock(return_value=created)

        state = MagicMock()
        state.get_pull_state.return_value = MagicMock(
            pr_number=1,
            head_sha="s",
            patch_sha256="p",
            comment_id=None,
            comment_url=None,
            reproducer=None,
        )

        pr = PullRequest(
            number=1,
            title="T",
            author_login="service-login",
            head_sha="s",
            updated_at=MagicMock(),
            html_url="h",
        )
        update = PullRequestUpdate(pr=pr, patch="p", patch_sha256="p")
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="s",
            patch_sha256="p",
            stacktrace="crash",
        )

        await report_result(
            github, state, update, reproducer, "rev123", "service-login"
        )

        github.create_issue_comment.assert_called_once()
        github.create_request_changes_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_reproduces_updates_comment_no_review(self) -> None:
        existing = IssueComment(
            id=5,
            html_url="https://old/comment",
            body=(f"{COMMENT_FIRST_LINE}\n<!-- llvm-hackme-state: bug_found -->"),
            author_login="service-login",
        )
        github = MagicMock(spec=GitHubClient)
        github.list_issue_comments = AsyncMock(return_value=[existing])
        github.update_issue_comment = AsyncMock(return_value=existing)

        old_reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="olds",
            patch_sha256="oldp",
            stacktrace="old crash",
        )
        state = MagicMock()
        state.get_pull_state.return_value = MagicMock(
            pr_number=1,
            head_sha="s",
            patch_sha256="p",
            comment_id=5,
            comment_url="https://old/comment",
            reproducer=old_reproducer,
        )

        pr = PullRequest(
            number=1,
            title="T",
            author_login="other-user",
            head_sha="s",
            updated_at=MagicMock(),
            html_url="h",
        )
        update = PullRequestUpdate(pr=pr, patch="p", patch_sha256="p")
        new_reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="s",
            patch_sha256="p",
            stacktrace="crash",
        )

        await report_result(
            github, state, update, new_reproducer, "rev123", "service-login"
        )

        github.update_issue_comment.assert_called_once()
        github.create_request_changes_review.assert_not_called()
        state.save_reproducer.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_issue_found_when_reproducer_gone(self) -> None:
        existing = IssueComment(
            id=5,
            html_url="https://old/comment",
            body=(f"{COMMENT_FIRST_LINE}\n<!-- llvm-hackme-state: bug_found -->"),
            author_login="service-login",
        )
        github = MagicMock(spec=GitHubClient)
        github.list_issue_comments = AsyncMock(return_value=[existing])
        github.update_issue_comment = AsyncMock(return_value=existing)

        old_reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=MagicMock(),
            command=["opt"],
            baseline_revision="r",
            pr_head_sha="olds",
            patch_sha256="oldp",
            stacktrace="old crash",
        )
        state = MagicMock()
        state.get_pull_state.return_value = MagicMock(
            pr_number=1,
            head_sha="s",
            patch_sha256="p",
            comment_id=5,
            comment_url="https://old/comment",
            reproducer=old_reproducer,
        )

        pr = PullRequest(
            number=1,
            title="T",
            author_login="other-user",
            head_sha="s",
            updated_at=MagicMock(),
            html_url="h",
        )
        update = PullRequestUpdate(pr=pr, patch="p", patch_sha256="p")

        await report_result(github, state, update, None, "rev123", "service-login")

        github.update_issue_comment.assert_called_once()
        call_args, _ = github.update_issue_comment.call_args
        assert call_args[0] == 5
        assert "no_issue_found_for_current_patch" in call_args[1]
        state.clear_reproducer.assert_called_once_with(1)
