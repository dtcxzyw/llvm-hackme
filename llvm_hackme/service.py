from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llvm_hackme.builds import BuildManager, ToolchainPaths
from llvm_hackme.commands import is_transient_error, set_command_log_path
from llvm_hackme.config import Config
from llvm_hackme.fuzzer import FuzzRunner
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.models import BugKind, PullRequest, PullRequestUpdate, Reproducer
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

        async def _wrapped() -> None:
            try:
                await self._handle_pr_update(update)
            finally:
                self._pr_tasks.pop(pr_number, None)

        self._pr_tasks[pr_number] = asyncio.create_task(_wrapped())

    async def _handle_pr_update(self, update: PullRequestUpdate) -> None:
        pr = update.pr
        pr_number = pr.number
        transient = False

        log_file = self._config.logs_dir / (
            f"pr-{pr_number}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)

        try:
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

            set_command_log_path(log_file)
            LOGGER.info("PR #%s debounce complete, starting processing", pr_number)
            await self._emit_status(pr, "in_progress")

            review = await self._reviewer.review(update.patch)
            if not review.accepted:
                LOGGER.info(
                    "OpenAI review rejected PR #%s: %s", pr_number, review.reason
                )
                await self._emit_status(pr, "review_rejected")
                return

            pass_name = guess_pass_name(update.patch)
            if pass_name is None:
                LOGGER.warning("Could not guess pass name for PR #%s", pr_number)
                return

            async with self._build_lock:
                try:
                    toolchain, tests_applied = await self._builds.prepare_pr_build(
                        update.patch, pr.head_sha
                    )
                except Exception:
                    LOGGER.exception("Failed to build PR #%s", pr_number)
                    return

            stored = self._state.get_pull_state(pr_number)
            if stored.reproducer is not None:
                try:
                    stored_opt = _opt_args_from_command(stored.reproducer.command)
                    verified_existing = await verify_reproducer(
                        stored.reproducer,
                        toolchain,
                        stored_opt,
                        memory_limit_bytes=self._config.opt_memory_limit_bytes,
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
                    self._state.save_reproducer(pr_number, verified_existing)
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

            has_tests = tests_applied

            verified: Reproducer | None = None
            if has_tests:
                fuzz_result = await self._fuzzer.run(
                    update.patch,
                    update.patch_sha256,
                    pr.head_sha,
                    toolchain,
                )
                reproducer = fuzz_result.reproducer
                if reproducer is not None:
                    try:
                        verified = await verify_reproducer(
                            reproducer,
                            toolchain,
                            [f"-passes={pass_name}"],
                            memory_limit_bytes=self._config.opt_memory_limit_bytes,
                        )
                    except Exception:
                        LOGGER.exception("Verification failed for PR #%s", pr_number)
                        verified = None

            if verified is None:
                hack_reproducer = await self._run_hack_agent(
                    update, toolchain, pass_name
                )
                if hack_reproducer is not None:
                    try:
                        opt_args = _opt_args_from_command(hack_reproducer.command)
                        verified = await verify_reproducer(
                            hack_reproducer,
                            toolchain,
                            opt_args,
                            memory_limit_bytes=self._config.opt_memory_limit_bytes,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Hack verification failed for PR #%s", pr_number
                        )
                        verified = None

            if verified is not None:
                await self._emit_status(pr, "bug_found")
                self._state.save_reproducer(pr_number, verified)
            else:
                await self._emit_status(pr, "passed")

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
                count = self._state.increment_retry(pr_number)
                max_retries = 3
                if count >= max_retries:
                    pending_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                    self._state.set_pending_until(pr_number, pending_until)
                    LOGGER.warning(
                        "PR #%s failed %d times, pending until %s",
                        pr_number,
                        count,
                        pending_until,
                    )
                    await self._emit_status(pr, "pending")
        finally:
            set_command_log_path(None)
            if not transient:
                self._state.reset_retry(pr_number)
                self._state.mark_processed(pr_number)

    async def _run_hack_agent(
        self,
        update: PullRequestUpdate,
        toolchain: ToolchainPaths,
        pass_name: str,
    ) -> Reproducer | None:
        config = self._config
        hack_dir = config.hack_work_dir
        hack_dir.mkdir(parents=True, exist_ok=True)
        for child in hack_dir.iterdir():
            if child.is_file():
                child.unlink()

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

        opencode_bin = _find_opencode()
        if opencode_bin is None:
            LOGGER.warning("opencode binary not found, skipping hack agent")
            self._cleanup_pipes(submit_pipe, response_pipe)
            return None

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
        opencode_log = open(  # noqa: ASYNC230,SIM115 — fd for subprocess
            str(hack_dir / log_name), "w"
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
            raise

        result_holder: dict[str, dict] = {}
        pipe_done = asyncio.Event()

        async def pipe_listener() -> None:
            try:

                def _read_pipe() -> str:
                    with open(submit_pipe, encoding="utf-8") as reader:
                        return reader.readline().strip()

                raw = await asyncio.to_thread(_read_pipe)
                if not raw:
                    return
                payload = json.loads(raw)
                hack_reproducer = await _hack_verify(
                    payload,
                    hack_dir,
                    toolchain,
                    update,
                    memory_limit_bytes=config.opt_memory_limit_bytes,
                )

                response = {"success": False, "reason": "unknown"}
                if hack_reproducer is not None:
                    response = {"success": True}
                    result_holder["reproducer"] = hack_reproducer

                with contextlib.suppress(OSError):

                    def _write_response() -> None:
                        with open(str(response_pipe), "w", encoding="utf-8") as wf:
                            wf.write(json.dumps(response) + "\n")

                    await asyncio.to_thread(_write_response)

                if response.get("success"):
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
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
        return result

    def _cleanup_pipes(self, *pipes: Path) -> None:
        for p in pipes:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)

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
                log_file = self._config.logs_dir / (
                    f"baseline-update-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
                )
                log_file.parent.mkdir(parents=True, exist_ok=True)
                set_command_log_path(log_file)
                async with self._build_lock:
                    revision = await self._builds.update_baseline()
                    LOGGER.info("Baseline updated to revision %s", revision)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Baseline update failed")
            finally:
                set_command_log_path(None)
            await asyncio.sleep(self._config.baseline_update_interval_seconds)


def _opt_args_from_command(command: list[str]) -> list[str]:
    for i, arg in enumerate(command):
        if arg == "-S" and i + 4 < len(command):
            return _normalize_opt_args(list(command[i + 4 :]))
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


def _find_opencode() -> str | None:
    import shutil

    which = shutil.which("opencode")
    if which:
        return which
    candidates = [
        Path.home() / ".opencode" / "bin" / "opencode",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


async def _hack_verify(
    payload: dict,
    hack_dir: Path,
    toolchain: ToolchainPaths,
    update: PullRequestUpdate,
    *,
    memory_limit_bytes: int | None = None,
) -> Reproducer | None:
    from llvm_hackme.verification import (
        check_crash,
        check_miscompilation,
        is_alive2_approximation,
    )

    ir_text = payload.get("ir", "")
    opt_args_str = payload.get("opt_args", "")
    kind_str = payload.get("kind", "crash")

    if not ir_text:
        return None

    opt_args = _normalize_opt_args(
        opt_args_str.split()
        if opt_args_str.strip()
        else ["-passes=instcombine<no-verify-fixpoint>"]
    )
    if not _valid_opt_args(opt_args):
        LOGGER.warning("Rejected unsafe opt_args from hack agent: %s", opt_args)
        return None

    src_file = hack_dir / "hack-reproducer.ll"
    src_file.write_text(ir_text)

    try:
        kind = BugKind(kind_str)
    except ValueError:
        kind = BugKind.CRASH

    if kind == BugKind.CRASH:
        baseline_result = await check_crash(
            toolchain.baseline_opt,
            src_file,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
        if baseline_result is not None:
            return None
        pr_result = await check_crash(
            toolchain.pr_opt,
            src_file,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
        if pr_result is None:
            return None
        return Reproducer(
            kind=BugKind.CRASH,
            source_path=src_file,
            command=[
                str(toolchain.pr_opt),
                "-S",
                "-o",
                "/dev/null",
                str(src_file),
                *opt_args,
            ],
            baseline_revision=toolchain.baseline_revision,
            pr_head_sha=update.pr.head_sha,
            patch_sha256=update.patch_sha256,
            stacktrace=pr_result.stacktrace,
            source_content=ir_text,
        )
    else:
        baseline_result = await check_miscompilation(
            toolchain.baseline_opt,
            toolchain.alive_tv,
            src_file,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
        if baseline_result is not None:
            return None
        pr_result = await check_miscompilation(
            toolchain.pr_opt,
            toolchain.alive_tv,
            src_file,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
        if pr_result is None or is_alive2_approximation(pr_result):
            return None
        return Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=src_file,
            command=[
                str(toolchain.pr_opt),
                "-S",
                "-o",
                "/dev/null",
                str(src_file),
                *opt_args,
            ],
            baseline_revision=toolchain.baseline_revision,
            pr_head_sha=update.pr.head_sha,
            patch_sha256=update.patch_sha256,
            alive2_counterexample=pr_result.alive2_output,
            source_content=ir_text,
        )
