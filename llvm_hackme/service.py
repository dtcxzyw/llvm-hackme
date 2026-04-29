from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llvm_hackme.builds import BuildManager, ToolchainPaths
from llvm_hackme.commands import (
    CommandError,
    append_command_log_message,
    find_opencode,
    is_transient_error,
    set_command_log_path,
)
from llvm_hackme.config import Config
from llvm_hackme.fuzzer import FuzzRunner
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.log_render import render_opencode_log
from llvm_hackme.models import BugKind, PullRequest, PullRequestUpdate, Reproducer
from llvm_hackme.passes import guess_pass_name
from llvm_hackme.reporting import report_result
from llvm_hackme.scanner import PullRequestScanner
from llvm_hackme.state import StateStore
from llvm_hackme.verification import verify_reproducer

LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[int, str, str, str, datetime], Awaitable[None]]


def _log_command_error(exc: BaseException, context: str) -> None:
    if isinstance(exc, CommandError):
        stderr = exc.result.stderr.strip()
        stdout = exc.result.stdout.strip()
        LOGGER.error(
            "%s — command stderr:\n%s\nstdout:\n%s",
            context,
            stderr[-4000:] if stderr else "(none)",
            stdout[-2000:] if stdout else "(none)",
        )
        append_command_log_message(
            f"--- {context} — FAILED (exit {exc.result.returncode}) ---"
        )


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
        self._pr_in_build: set[int] = set()
        self._service_login = service_login
        self._status_callback = status_callback
        self.baseline_revision: str | None = None
        self.alive2_revision: str | None = None
        self.baseline_updated_at: datetime | None = None

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
            self._log_cleanup_loop(),
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
            if pr_number in self._pr_in_build:
                LOGGER.info(
                    "PR #%s is currently in build phase, skipping cancel",
                    pr_number,
                )
                self._pr_tasks[pr_number] = existing
                return
            existing.cancel()
            LOGGER.info(
                "Cancelled existing task for PR #%s (new update arrived)", pr_number
            )

        task = asyncio.create_task(self._handle_pr_update(update))
        self._pr_tasks[pr_number] = task

        def _done_callback(t: asyncio.Task[object]) -> None:
            if self._pr_tasks.get(pr_number) is t:
                self._pr_tasks.pop(pr_number, None)

        task.add_done_callback(_done_callback)

    async def request_pr(self, pr_number: int) -> None:
        try:
            pr = await self._github.get_pull_request(pr_number)
        except Exception:
            LOGGER.exception("Failed to fetch PR #%s", pr_number)
            return
        try:
            patch = await self._github.get_pull_patch(pr_number)
        except Exception:
            LOGGER.exception("Failed to fetch patch for PR #%s", pr_number)
            return
        patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
        self._state.record_pr_update(
            pr_number, head_sha=pr.head_sha, patch_sha256=patch_sha256
        )
        update = PullRequestUpdate(pr, patch, patch_sha256)
        self._schedule_pr_task(update)
        LOGGER.info("Manually enqueued PR #%s", pr_number)

    async def _check_pr_stale(
        self, pr_number: int, processed_sha: str, update: PullRequestUpdate
    ) -> bool:
        try:
            current_sha = await self._github.get_pull_head_sha(pr_number)
        except Exception:
            LOGGER.exception("Failed to re-fetch PR #%s head SHA", pr_number)
            return False
        if current_sha == processed_sha:
            return False
        LOGGER.info(
            "PR #%s head SHA changed during processing (%s → %s), re-queuing",
            pr_number,
            processed_sha[:8],
            current_sha[:8],
        )
        try:
            patch = await self._github.get_pull_patch(pr_number)
        except Exception:
            LOGGER.exception("Failed to fetch updated patch for PR #%s", pr_number)
            return False
        patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
        new_pr = PullRequest(
            number=update.pr.number,
            title=update.pr.title,
            author_login=update.pr.author_login,
            head_sha=current_sha,
            updated_at=datetime.now(timezone.utc),
            html_url=update.pr.html_url,
        )
        new_update = PullRequestUpdate(
            pr=new_pr, patch=patch, patch_sha256=patch_sha256
        )
        # NOTE: state is intentionally not updated here via record_pr_update().
        # The enqueued task will call mark_processed() on completion, and the
        # next scanner cycle will naturally pick up the new head_sha.  Updating
        # the DB here would risk overwriting a concurrently-set processed_at.
        self._schedule_pr_task(new_update)
        return True

    async def _handle_pr_update(self, update: PullRequestUpdate) -> None:
        pr = update.pr
        pr_number = pr.number
        transient = False

        log_file = self._config.logs_dir / (
            f"pr-{pr_number}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)

        fuzz_mutation_count = 0
        hack_submissions: list[dict] = []
        verified: Reproducer | None = None
        pass_name: str | None = None

        try:
            set_command_log_path(log_file)
            LOGGER.info("PR #%s processing started", pr_number)
            await self._emit_status(pr, "in_progress")

            review = await self._reviewer.review(update.patch)
            if not review.accepted:
                LOGGER.info(
                    "OpenAI review rejected PR #%s: %s", pr_number, review.reason
                )
                await self._emit_status(pr, "review_rejected")
                return

            pass_name = guess_pass_name(update.patch)
            # Intentionally skip PRs whose changed files do not match any
            # known pass keyword — there is nothing to fuzz or hack on.
            if pass_name is None:
                LOGGER.warning("Could not guess pass name for PR #%s", pr_number)
                await self._emit_status(pr, "passed")
                return

            await self._emit_status(pr, "waiting_for_build_lock")
            async with self._build_lock:
                self._pr_in_build.add(pr_number)
                try:
                    await self._emit_status(pr, "building")
                    try:
                        (
                            baseline_revision,
                            full_patch_applied,
                        ) = await self._builds.prepare_pr_worktree(
                            update.patch, pr.head_sha
                        )
                    except Exception:
                        _log_command_error(
                            sys.exc_info()[1],
                            f"Failed to prepare PR worktree #{pr_number}",
                        )
                        LOGGER.exception("Failed to prepare PR worktree #%s", pr_number)
                        transient = True
                        await self._maybe_backoff(pr_number, pr)
                        return

                    try:
                        await self._builds.build_pr_opt()
                    except Exception:
                        _log_command_error(
                            sys.exc_info()[1],
                            f"Failed to build PR opt #{pr_number}",
                        )
                        LOGGER.exception("Failed to build PR opt #%s", pr_number)
                        transient = True
                        await self._maybe_backoff(pr_number, pr)
                        return

                    toolchain = self._builds.toolchain_paths(baseline_revision)

                    await self._emit_status(pr, "fuzzing")

                    stored = self._state.get_pull_state(pr_number)
                    if stored.reproducer is not None:
                        try:
                            stored_opt = _opt_args_from_command(
                                stored.reproducer.command
                            )
                            verified_existing, _ = await verify_reproducer(
                                stored.reproducer,
                                toolchain,
                                stored_opt,
                                memory_limit_bytes=self._config.opt_memory_limit_bytes,
                            )
                        except Exception:
                            LOGGER.exception(
                                "Re-verification of existing reproducer"
                                " failed for PR #%s",
                                pr_number,
                            )
                            verified_existing = None
                        if verified_existing is not None:
                            LOGGER.info(
                                "PR #%s: existing reproducer still reproduces",
                                pr_number,
                            )
                            await self._emit_status(pr, "bug_found")
                            self._state.save_reproducer(pr_number, verified_existing)
                            if await self._check_pr_stale(
                                pr_number, pr.head_sha, update
                            ):
                                return
                            await report_result(
                                self._github,
                                self._state,
                                update,
                                verified_existing,
                                toolchain.baseline_revision,
                                self._service_login,
                            )
                            return
                        LOGGER.info(
                            "PR #%s: existing reproducer no longer reproduces,"
                            " running new fuzz",
                            pr_number,
                        )

                    if full_patch_applied:
                        fuzz_result = await self._fuzzer.run(
                            update.patch,
                            update.patch_sha256,
                            pr.head_sha,
                            toolchain,
                        )
                        fuzz_mutation_count = fuzz_result.mutation_count
                        reproducer = fuzz_result.reproducer
                        if reproducer is not None:
                            try:
                                verified, _ = await verify_reproducer(
                                    reproducer,
                                    toolchain,
                                    [f"-passes={pass_name}"],
                                    memory_limit_bytes=(
                                        self._config.opt_memory_limit_bytes
                                    ),
                                )
                            except Exception:
                                LOGGER.exception(
                                    "Verification failed for PR #%s", pr_number
                                )
                                verified = None

                    if verified is None:
                        await self._emit_status(pr, "hacking")
                        verified, hack_submissions = await self._run_hack_agent(
                            update, toolchain, pass_name
                        )
                finally:
                    self._pr_in_build.discard(pr_number)

            if verified is not None:
                await self._emit_status(pr, "bug_found")
                self._state.save_reproducer(pr_number, verified)
            else:
                await self._emit_status(pr, "passed")

            if not (await self._check_pr_stale(pr_number, pr.head_sha, update)):
                await report_result(
                    self._github,
                    self._state,
                    update,
                    verified,
                    toolchain.baseline_revision,
                    self._service_login,
                )

            LOGGER.info("PR #%s processing complete", pr_number)
        except asyncio.CancelledError:
            raise
        except Exception:
            exc = sys.exc_info()[1]
            LOGGER.exception("Unhandled error processing PR #%s", pr_number)
            if exc is not None and (
                is_transient_error(exc) or (hasattr(exc, "retryable") and exc.retryable)  # type: ignore[union-attr]
            ):
                transient = True
                await self._maybe_backoff(pr_number, pr)
        finally:
            eval_summary = {
                "pr_number": pr_number,
                "head_sha": pr.head_sha[:12],
                "pass_name": pass_name,
                "fuzz_mutation_count": fuzz_mutation_count,
                "hack_submission_count": len(hack_submissions),
                "hack_submissions": hack_submissions,
                "result": "bug_found" if verified is not None else "passed",
            }
            if verified is not None:
                eval_summary["bug_kind"] = verified.kind.value
            append_command_log_message(json.dumps(eval_summary))

            set_command_log_path(None)
            if not transient:
                self._state.reset_retry(pr_number)
                self._state.mark_processed(pr_number)

    async def _run_hack_agent(
        self,
        update: PullRequestUpdate,
        toolchain: ToolchainPaths,
        pass_name: str,
    ) -> tuple[Reproducer | None, list[dict]]:
        config = self._config
        hack_dir = config.hack_work_dir
        hack_dir.mkdir(parents=True, exist_ok=True)

        patch_file = hack_dir / "patch.diff"
        patch_file.write_text(update.patch)

        context = {
            "patch_file": str(patch_file),
            "pass_name": pass_name,
            "work_dir": str(hack_dir),
            "baseline_opt": str(toolchain.baseline_opt),
            "pr_opt": str(toolchain.pr_opt),
            "alive_tv": str(toolchain.alive_tv),
            "baseline_src_dir": str(config.llvm_project_dir),
            "pr_src_dir": str(config.llvm_project_pr_dir),
            "opt_memory_limit_bytes": config.opt_memory_limit_bytes,
            "suggested_opt_args": f"-passes={pass_name}",
        }
        config.hack_context_file.write_text(json.dumps(context))

        submit_pipe = hack_dir / "submit.pipe"
        response_pipe = hack_dir / "response.pipe"

        for p in (submit_pipe, response_pipe):
            p.unlink(missing_ok=True)
            os.mkfifo(str(p))

        opencode_bin = find_opencode()
        if opencode_bin is None:
            LOGGER.warning("opencode binary not found, skipping hack agent")
            self._cleanup_pipes(submit_pipe, response_pipe)
            return None, []

        hack_prompt = (
            "You are the hack agent.  Use the `hack_context` tool first to get "
            "all paths and configuration, then analyze the patch diff file to find "
            "a crash or miscompilation regression.  When you find one, call "
            "`hack_submit` with the IR, opt_args, kind, and description.  "
            "Work quickly and submit as soon as you have a credible candidate."
        )

        LOGGER.info("Launching hack agent for PR #%s", update.pr.number)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_name = f"opencode-pr{update.pr.number}-{ts}.log"
        logs_dir = config.logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        opencode_log_path = logs_dir / log_name
        opencode_log = open(  # noqa: ASYNC230,SIM115 — fd for subprocess
            str(opencode_log_path), "w"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                str(opencode_bin),
                "run",
                "--agent",
                "hack",
                "--model",
                config.hack_model,
                "--format",
                "json",
                "--thinking",
                hack_prompt,
                env={
                    **os.environ,
                    "HACK_CONTEXT_FILE": str(config.hack_context_file),
                    "HACK_SUBMIT_PIPE": str(submit_pipe),
                    "HACK_RESPONSE_PIPE": str(response_pipe),
                },
                stdout=opencode_log,
                stderr=opencode_log,
            )
        except Exception:
            opencode_log.close()
            self._cleanup_pipes(submit_pipe, response_pipe)
            _render_recent_log(opencode_log_path)
            raise

        result_holder: dict[str, Reproducer | None] = {}
        submissions: list[dict] = []
        pipe_done = asyncio.Event()

        async def pipe_listener() -> None:
            try:

                def _read_pipe() -> str:
                    with open(submit_pipe, encoding="utf-8") as reader:
                        return reader.readline().strip()

                while True:
                    raw = await asyncio.to_thread(_read_pipe)
                    if not raw:
                        break
                    payload = json.loads(raw)
                    sub_record: dict = {
                        "kind": payload.get("kind", "crash"),
                        "description": payload.get("description", ""),
                        "verified": False,
                    }
                    hack_reproducer, reason = await _hack_verify(
                        payload,
                        hack_dir,
                        toolchain,
                        update,
                        memory_limit_bytes=config.opt_memory_limit_bytes,
                    )

                    response: dict = {"success": False, "reason": reason or "unknown"}
                    if hack_reproducer is not None:
                        response = {"success": True}
                        result_holder["reproducer"] = hack_reproducer
                        sub_record["verified"] = True

                    submissions.append(sub_record)

                    response_json = json.dumps(response) + "\n"

                    with contextlib.suppress(OSError):

                        def _write_response(data: str = response_json) -> None:
                            with open(str(response_pipe), "w", encoding="utf-8") as wf:
                                wf.write(data)

                        await asyncio.to_thread(_write_response)

                    if response.get("success"):
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        break
            except Exception:
                LOGGER.exception("Hack pipe listener failed")
            finally:
                pipe_done.set()

        pipe_task = asyncio.create_task(pipe_listener())

        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=config.hack_budget_seconds,
            )
        except asyncio.TimeoutError:
            LOGGER.info("Hack agent timed out for PR #%s", update.pr.number)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        finally:
            if not pipe_task.done():
                pipe_task.cancel()
                for pipe, mode in [
                    (submit_pipe, os.O_WRONLY),
                    (response_pipe, os.O_RDONLY),
                ]:
                    with contextlib.suppress(OSError):
                        fd = os.open(str(pipe), mode | os.O_NONBLOCK)
                        os.close(fd)
            try:
                await asyncio.wait_for(pipe_done.wait(), timeout=30)
            except asyncio.TimeoutError:
                LOGGER.warning("Timed out waiting for hack pipe to close")
            self._cleanup_pipes(submit_pipe, response_pipe)
            opencode_log.close()

        result = result_holder.get("reproducer")
        if result is not None:
            LOGGER.info("Hack agent found bug for PR #%s", update.pr.number)
        _render_recent_log(opencode_log_path, result, submissions)
        return result, submissions

    def _cleanup_pipes(self, *pipes: Path) -> None:
        for p in pipes:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)

    async def _emit_status(self, pr: PullRequest, status: str) -> None:
        if self._status_callback is None:
            return
        try:
            await self._status_callback(
                pr.number, pr.title, pr.html_url, status, pr.updated_at
            )
        except Exception:
            LOGGER.warning("Status callback failed", exc_info=True)

    async def _maybe_backoff(self, pr_number: int, pr: PullRequest) -> None:
        count = self._state.increment_retry(pr_number)
        if count >= 3:
            pending_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            self._state.set_pending_until(pr_number, pending_until)
            LOGGER.warning(
                "PR #%d failed %d times, pending until %s",
                pr_number,
                count,
                pending_until,
            )
            await self._emit_status(pr, "pending")

    async def _baseline_update_loop(self) -> None:
        while True:
            try:
                log_file = self._config.logs_dir / (
                    f"baseline-update-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
                )
                log_file.parent.mkdir(parents=True, exist_ok=True)
                set_command_log_path(log_file)

                async with self._build_lock:
                    (
                        old_llvm,
                        old_alive2,
                        revision,
                    ) = await self._builds.sync_baseline_sources()

                    try:
                        await self._builds.build_baseline_toolchain()
                    except Exception:
                        _log_command_error(sys.exc_info()[1], "Baseline build failed")
                        LOGGER.exception(
                            "Baseline build failed, rolling back %s → %s and %s → %s",
                            self._config.llvm_project_dir,
                            old_llvm,
                            self._config.alive2_dir,
                            old_alive2,
                        )
                        await self._builds.rollback_sources(old_llvm, old_alive2)
                        await self._builds.build_baseline_toolchain()
                        raise

                alive2_rev = await self._builds.current_alive2_revision()
                self.baseline_revision = revision
                self.alive2_revision = alive2_rev
                self.baseline_updated_at = datetime.now(timezone.utc)
                LOGGER.info(
                    "Baseline updated to LLVM %s, Alive2 %s",
                    revision[:12],
                    alive2_rev[:12],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Baseline update failed")
            finally:
                set_command_log_path(None)
            await asyncio.sleep(self._config.baseline_update_interval_seconds)

    async def _log_cleanup_loop(self) -> None:
        retention = timedelta(days=10)
        patterns = ("pr-*.log", "baseline-update-*.log", "opencode-pr*.log")
        while True:
            try:
                now = datetime.now(timezone.utc)
                cutoff = now - retention
                logs_dir = self._config.logs_dir
                if not logs_dir.is_dir():
                    continue
                for pattern in patterns:
                    for log_file in sorted(logs_dir.glob(pattern)):
                        try:
                            mtime = datetime.fromtimestamp(
                                log_file.stat().st_mtime, tz=timezone.utc
                            )
                            if mtime < cutoff:
                                log_file.unlink()
                                LOGGER.debug("Cleaned up old log: %s", log_file.name)
                        except OSError:
                            pass
            except Exception:
                LOGGER.exception("Log cleanup iteration failed")
            await asyncio.sleep(3600)


def _render_recent_log(
    json_path: Path,
    hack_result: Reproducer | None = None,
    hack_submissions: list[dict] | None = None,
) -> None:
    try:
        txt_path = render_opencode_log(json_path)
        LOGGER.debug("Rendered opencode log → %s", txt_path.name)
        if hack_submissions:
            _append_summary(txt_path, hack_result, hack_submissions)
    except Exception:
        LOGGER.exception("Failed to render opencode log %s", json_path.name)


def _append_summary(
    txt_path: Path, result: Reproducer | None, submissions: list[dict]
) -> None:
    lines = ["", "=" * 40, "ANALYSIS RESULTS", "=" * 40]
    if result is not None:
        lines.append(f"Bug found: {result.kind.value}")
        if result.stacktrace:
            lines.append("Stacktrace:")
            lines.append(result.stacktrace[:2000])
        if result.alive2_counterexample:
            lines.append("Alive2 output:")
            lines.append(result.alive2_counterexample[:2000])
    else:
        reason = f"no bug found in {len(submissions)} submissions"
        if submissions:
            kinds = {s.get("kind", "?") for s in submissions}
            verified = sum(1 for s in submissions if s.get("verified"))
            reason += f", attempted kinds: {kinds}, verified: {verified}"
        lines.append(f"Result: {reason}")
    with txt_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _opt_args_from_command(command: list[str]) -> list[str]:
    for i, arg in enumerate(command):
        if arg.endswith(".ll") and not arg.startswith("-"):
            return _normalize_opt_args(list(command[i + 1 :]))
    return ["-passes=instcombine<no-verify-fixpoint>"]


def _normalize_opt_args(opt_args: list[str]) -> list[str]:
    return [_fixup_instcombine(a) for a in opt_args]


def _fixup_instcombine(arg: str) -> str:
    if arg == "instcombine":
        return "instcombine<no-verify-fixpoint>"
    if arg.startswith("-passes="):
        return _replace_pass(arg, "instcombine", "instcombine<no-verify-fixpoint>")
    return arg


def _replace_pass(passes_arg: str, old: str, new: str) -> str:
    prefix = passes_arg.removeprefix("-passes=")
    parts = prefix.split(",")
    fixed = [new if p == old else p for p in parts]
    return "-passes=" + ",".join(fixed)


_VALID_OPT_ARG_RE = re.compile(r"^-passes=[-\w<>\[\]#,&]+$")


def _valid_opt_args(opt_args: list[str]) -> bool:
    return all(_VALID_OPT_ARG_RE.match(arg) for arg in opt_args)


async def _hack_verify(
    payload: dict,
    hack_dir: Path,
    toolchain: ToolchainPaths,
    update: PullRequestUpdate,
    *,
    memory_limit_bytes: int | None = None,
) -> tuple[Reproducer | None, str]:
    ir_text = payload.get("ir", "")
    opt_args_str = payload.get("opt_args", "")
    kind_str = payload.get("kind", "crash")
    alive2_args_str = payload.get("alive2_args", "")

    if not ir_text:
        return None, "Missing IR text"

    opt_args = _normalize_opt_args(
        opt_args_str.split()
        if opt_args_str.strip()
        else ["-passes=instcombine<no-verify-fixpoint>"]
    )
    if not _valid_opt_args(opt_args):
        reason = f"Rejected unsafe opt_args: {opt_args}"
        LOGGER.warning(reason)
        return None, reason

    alive2_extra_args = (
        alive2_args_str.strip().split() if alive2_args_str.strip() else None
    )

    try:
        kind = BugKind(kind_str)
    except ValueError:
        kind = BugKind.CRASH

    candidate = Reproducer(
        kind=kind,
        source_path=hack_dir / "hack-reproducer.ll",
        command=[
            str(toolchain.pr_opt),
            "-S",
            "-o",
            "/dev/null",
            str(hack_dir / "hack-reproducer.ll"),
            *opt_args,
        ],
        baseline_revision=toolchain.baseline_revision,
        pr_head_sha=update.pr.head_sha,
        patch_sha256=update.patch_sha256,
        source_content=ir_text,
    )

    return await verify_reproducer(
        candidate,
        toolchain,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
        alive2_extra_args=alive2_extra_args,
    )
