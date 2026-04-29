from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.tui import HackmeTUI, PREntry

_FIXED_TS = datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)


class TestPREntry:
    def test_format_line_in_progress(self) -> None:
        entry = PREntry(
            pr_number=42,
            pr_url="https://gh/pr/42",
            pr_title="Fix foo",
            status="in_progress",
            updated_at=_FIXED_TS,
        )
        line = entry.format_line()
        assert "IN PROGRESS" in line
        assert "2026-04-28 14:30" in line
        assert "#42" in line
        assert "Fix foo" in line

    def test_format_line_bug_found(self) -> None:
        entry = PREntry(
            pr_number=7,
            pr_url="https://gh/pr/7",
            pr_title="Fix bar",
            status="bug_found",
            updated_at=_FIXED_TS,
        )
        line = entry.format_line()
        assert "BUG FOUND" in line
        assert "2026-04-28 14:30" in line
        assert "#7" in line
        assert "Fix bar" in line

    def test_format_line_passed(self) -> None:
        entry = PREntry(
            pr_number=100,
            pr_url="https://gh/pr/100",
            pr_title="Fix baz",
            status="passed",
            updated_at=_FIXED_TS,
        )
        line = entry.format_line()
        assert "PASSED" in line
        assert "2026-04-28 14:30" in line
        assert "#100" in line
        assert "Fix baz" in line

    def test_format_line_unknown_status(self) -> None:
        entry = PREntry(
            pr_number=1,
            pr_url="https://gh/pr/1",
            pr_title="X",
            status="custom",
            updated_at=_FIXED_TS,
        )
        assert "custom" in entry.format_line()
        assert "2026-04-28 14:30" in entry.format_line()

    def test_format_line_empty_status_fallback(self) -> None:
        entry = PREntry(
            pr_number=5,
            pr_url="",
            pr_title="T",
            status="",
            updated_at=_FIXED_TS,
        )
        assert "???" in entry.format_line()


class TestTUIInit:
    def test_constructor_sets_attributes(self) -> None:
        config = MagicMock()
        state = MagicMock()
        github = MagicMock(spec=GitHubClient)
        reviewer = MagicMock(spec=OpenAIPatchReviewer)

        app = HackmeTUI(config, state, github, reviewer)
        assert app._config is config
        assert app._state is state
        assert app._github is github
        assert app._reviewer is reviewer
        assert app._login == ""
        assert app._pr_entries == {}

    def test_resolve_login_with_override(self) -> None:
        config = MagicMock()
        config.github_login_override = "bot-user"
        app = HackmeTUI(config, MagicMock(), MagicMock(), MagicMock())
        assert app._resolve_login() == "bot-user"

    def test_resolve_login_from_attr(self) -> None:
        config = MagicMock()
        config.github_login_override = None
        app = HackmeTUI(config, MagicMock(), MagicMock(), MagicMock())
        app._login = "queried-user"
        assert app._resolve_login() == "queried-user"


class TestRefreshHeader:
    def test_header_without_service(self) -> None:
        config = MagicMock()
        config.github_login_override = "bot"
        app = HackmeTUI(config, MagicMock(), MagicMock(), MagicMock())
        app._pr_entries = {42: MagicMock()}
        header = MagicMock()
        app.query_one = MagicMock(return_value=header)
        app._refresh_header()
        content = header.update.call_args[0][0]
        assert "Tracked: 1 PRs" in content
        assert "Login: bot" in content
        assert "LLVM:" not in content

    def test_header_with_versions(self) -> None:
        from datetime import datetime, timezone

        config = MagicMock()
        config.github_login_override = "bot"
        app = HackmeTUI(config, MagicMock(), MagicMock(), MagicMock())
        app._pr_entries = {}
        svc = MagicMock()
        svc.baseline_revision = "a" * 40
        svc.alive2_revision = "b" * 40
        svc.baseline_updated_at = datetime(2026, 4, 29, 10, 30, tzinfo=timezone.utc)
        app._service = svc
        header = MagicMock()
        app.query_one = MagicMock(return_value=header)
        app._refresh_header()
        content = header.update.call_args[0][0]
        assert "LLVM: aaaaaaaa" in content
        assert "Alive2: bbbbbbbb" in content
        assert "Updated: 2026-04-29 10:30" in content
        assert "Tracked: 0 PRs" in content

    def test_header_only_llvm(self) -> None:
        config = MagicMock()
        config.github_login_override = "bot"
        app = HackmeTUI(config, MagicMock(), MagicMock(), MagicMock())
        svc = MagicMock()
        svc.baseline_revision = "c" * 40
        svc.alive2_revision = None
        svc.baseline_updated_at = None
        app._service = svc
        header = MagicMock()
        app.query_one = MagicMock(return_value=header)
        app._refresh_header()
        content = header.update.call_args[0][0]
        assert "LLVM: cccccccc" in content
        assert "Alive2:" not in content
