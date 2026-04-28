from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.widgets import RichLog, Static

from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.service import HackmeService
from llvm_hackme.state import StateStore


class _RichLogHandler(logging.Handler):
    def __init__(self, app: App[None], fmt: str | None = None) -> None:
        super().__init__()
        self._app = app
        if fmt is not None:
            self.setFormatter(logging.Formatter(fmt))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            log_panel = self._app.query_one("#log-panel", RichLog)
            log_panel.write(msg)
        except Exception:
            self.handleError(record)


STATUS_LABELS: dict[str, str] = {
    "waiting_for_build_lock": "WAITING FOR BUILD",
    "building": "BUILDING",
    "fuzzing": "FUZZING",
    "in_progress": "IN PROGRESS",
    "bug_found": "BUG FOUND",
    "passed": "PASSED",
    "review_rejected": "REVIEW REJECTED",
    "pending": "PENDING",
}


@dataclass
class PREntry:
    pr_number: int
    pr_url: str
    pr_title: str
    status: str
    updated_at: datetime

    def format_line(self) -> str:
        label = STATUS_LABELS.get(self.status, self.status) or "???"
        ts = self.updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        return f"{label} #{self.pr_number} {ts} {self.pr_title}"


class HackmeTUI(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #status-header {
        height: 1;
        content-align: center middle;
        background: $surface;
    }

    #pr-panel {
        height: 12;
    }

    #log-panel {
        height: 1fr;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(
        self,
        config: Config,
        state: StateStore,
        github: GitHubClient,
        reviewer: OpenAIPatchReviewer,
    ) -> None:
        super().__init__()
        self._config = config
        self._state = state
        self._github = github
        self._reviewer = reviewer
        self._login = ""
        self._pr_entries: dict[int, PREntry] = {}
        self._service_task: asyncio.Task[object] | None = None

    def _resolve_login(self) -> str:
        override = self._config.github_login_override
        if override:
            return override
        return self._login

    def compose(self) -> ComposeResult:
        yield Static("", id="status-header")
        yield Static("", id="pr-panel")
        yield RichLog(id="log-panel", wrap=True, highlight=True, markup=True)

    async def on_mount(self) -> None:
        override = self._config.github_login_override
        if not override:
            self._login = await self._github.get_authenticated_login()

        self._refresh_header()
        self._refresh_pr_panel()

        handler = _RichLogHandler(
            self, fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        logging.getLogger().addHandler(handler)

        async def _status_callback(
            pr_number: int,
            pr_title: str,
            pr_url: str,
            status: str,
            updated_at: datetime,
        ) -> None:
            self._pr_entries[pr_number] = PREntry(
                pr_number, pr_url, pr_title, status, updated_at
            )

        service = HackmeService(
            self._config,
            self._state,
            self._github,
            self._reviewer,
            status_callback=_status_callback,
            service_login=self._resolve_login(),
        )
        self._service_task = asyncio.create_task(service.run_forever())

        self.set_interval(0.5, self._refresh_ui)

    def _refresh_ui(self) -> None:
        self._refresh_header()
        self._refresh_pr_panel()

    def _refresh_header(self) -> None:
        count = len(self._pr_entries)
        self.query_one("#status-header", Static).update(
            f"Tracked: {count} PRs   Login: {self._resolve_login()}"
        )

    def _refresh_pr_panel(self) -> None:
        if not self._pr_entries:
            self.query_one("#pr-panel", Static).update("No PRs processed yet.")
            return
        try:
            entries = sorted(
                self._pr_entries.values(), key=lambda e: e.updated_at, reverse=True
            )[:10]
            lines = [entry.format_line() for entry in entries]
            self.query_one("#pr-panel", Static).update("\n".join(lines))
        except Exception:
            logging.exception("Failed to refresh PR panel")
