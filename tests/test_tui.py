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
        assert "PR #   42" in line
        assert "IN PROGRESS" in line
        assert "https://gh/pr/42" in line
        assert "2026-04-28 14:30" in line

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
        assert "https://gh/pr/7" in line
        assert "2026-04-28 14:30" in line

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
