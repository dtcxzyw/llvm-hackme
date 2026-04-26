from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from llvm_hackme.builds import BuildManager
from llvm_hackme.config import Config
from llvm_hackme.fuzzer import FuzzRunner
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.models import PullRequest, PullRequestUpdate
from llvm_hackme.passes import guess_pass_name
from llvm_hackme.reporting import report_result
from llvm_hackme.scanner import PullRequestScanner
from llvm_hackme.state import StateStore
from llvm_hackme.verification import verify_reproducer

LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[int, str, str, str], Awaitable[None]]


class HackmeService:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        github: GitHubClient,
        reviewer: OpenAIPatchReviewer,
        *,
        status_callback: StatusCallback | None = None,
        service_login: str | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._github = github
        self._reviewer = reviewer
        self._scanner = PullRequestScanner(config, state, github)
        self._builds = BuildManager(config)
        self._fuzzer = FuzzRunner(config)
        self._build_lock = asyncio.Lock()
        self._pr_tasks: dict[int, asyncio.Task[object]] = {}
        self._service_login = service_login
        self._status_callback = status_callback

    async def run_forever(self) -> None:
        config = self._config
        if self._service_login is None:
            if config.github_login_override:
                self._service_login = config.github_login_override
                LOGGER.info("Using overridden GitHub login: %s", self._service_login)
            else:
                self._service_login = await self._github.get_authenticated_login()
                LOGGER.info("Authenticated GitHub login: %s", self._service_login)

        await asyncio.gather(
            self._scan_loop(),
            self._baseline_update_loop(),
        )

    async def _scan_loop(self) -> None:
        while True:
            try:
                await self._scan_once()
            except Exception:
                LOGGER.exception("Scan loop iteration failed")
            await asyncio.sleep(self._config.scan_interval_seconds)

    async def _scan_once(self) -> None:
        updates = await self._scanner.scan_once()
        for update in updates:
            self._schedule_pr_task(update)

    def _schedule_pr_task(self, update: PullRequestUpdate) -> None:
        pr_number = update.pr.number
        existing = self._pr_tasks.pop(pr_number, None)
        if existing is not None and not existing.done():
            existing.cancel()
            LOGGER.info(
                "Cancelled existing task for PR #%s (new update arrived)", pr_number
            )

        self._pr_tasks[pr_number] = asyncio.create_task(self._handle_pr_update(update))

    async def _handle_pr_update(self, update: PullRequestUpdate) -> None:
        pr = update.pr
        pr_number = pr.number
        LOGGER.info(
            "PR #%s update queued, waiting %s debounce seconds",
            pr_number,
            self._config.debounce_seconds,
        )
        try:
            await asyncio.sleep(self._config.debounce_seconds)
        except asyncio.CancelledError:
            LOGGER.info("PR #%s task cancelled during debounce", pr_number)
            raise

        LOGGER.info("PR #%s debounce complete, starting processing", pr_number)
        await self._emit_status(pr, "in_progress")

        review = await self._reviewer.review(update.patch)
        if not review.accepted:
            LOGGER.info("OpenAI review rejected PR #%s: %s", pr_number, review.reason)
            await self._emit_status(pr, "review_rejected")
            return

        pass_name = guess_pass_name(update.patch)
        if pass_name is None:
            LOGGER.warning("Could not guess pass name for PR #%s", pr_number)
            return

        async with self._build_lock:
            try:
                toolchain = await self._builds.prepare_pr_build(
                    update.patch, pr.head_sha
                )
            except Exception:
                LOGGER.exception("Failed to build PR #%s", pr_number)
                return

            stored = self._state.get_pull_state(pr_number)
            if stored.reproducer is not None:
                try:
                    verified_existing = await verify_reproducer(
                        stored.reproducer, toolchain, pass_name
                    )
                except Exception:
                    LOGGER.exception(
                        "Re-verification of existing reproducer failed for PR #%s",
                        pr_number,
                    )
                    verified_existing = None
                if verified_existing is not None:
                    LOGGER.info(
                        "PR #%s: existing reproducer still reproduces", pr_number
                    )
                    await self._emit_status(pr, "bug_found")
                    await report_result(
                        self._github,
                        self._state,
                        update,
                        verified_existing,
                        toolchain.baseline_revision,
                        self._service_login,
                    )
                    self._state.mark_processed(pr_number)
                    return
                LOGGER.info(
                    "PR #%s: existing reproducer no longer reproduces,"
                    " running new fuzz",
                    pr_number,
                )

            fuzz_result = await self._fuzzer.run(
                update.patch,
                update.patch_sha256,
                pr.head_sha,
                toolchain,
            )

            reproducer = fuzz_result.reproducer
            if reproducer is not None:
                try:
                    verified = await verify_reproducer(reproducer, toolchain, pass_name)
                except Exception:
                    LOGGER.exception("Verification failed for PR #%s", pr_number)
                    verified = None
            else:
                verified = None

            baseline_revision = toolchain.baseline_revision

        if verified is not None:
            await self._emit_status(pr, "bug_found")
        else:
            await self._emit_status(pr, "passed")

        await report_result(
            self._github,
            self._state,
            update,
            verified,
            baseline_revision,
            self._service_login,
        )

        LOGGER.info("PR #%s processing complete", pr_number)
        self._state.mark_processed(pr_number)

    async def _emit_status(self, pr: PullRequest, status: str) -> None:
        if self._status_callback is None:
            return
        try:
            await self._status_callback(pr.number, pr.title, pr.html_url, status)
        except Exception:
            LOGGER.debug("Status callback failed", exc_info=True)

    async def _baseline_update_loop(self) -> None:
        while True:
            try:
                async with self._build_lock:
                    revision = await self._builds.update_baseline()
                    LOGGER.info("Baseline updated to revision %s", revision)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Baseline update failed")
            await asyncio.sleep(self._config.baseline_update_interval_seconds)
