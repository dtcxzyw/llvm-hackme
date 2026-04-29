from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import (
    CommandError,
    CommandResult,
    is_disk_full_output,
    minimal_execution_env,
    run_command,
)
from llvm_hackme.models import BugKind, Reproducer

_FORBIDDEN_FASTMATH_RE = re.compile(
    r"^\s*(?:%\w+\s*=\s*)?"
    r"(?:fadd|fsub|fmul|fdiv|frem|fcmp|call)\b"
    r".*?\b(fast|nsz|arcp|contract|afn|reassoc)\b",
    re.MULTILINE,
)

LOGGER = logging.getLogger(__name__)  # noqa: F401 — used by _verify_regression_*

VERIFY_TIMEOUT = 120
ALIVE2_INCORRECT_RE = re.compile(
    r"[1-9]\d* incorrect transformations?|ERROR: Value mismatch"
)


def _output_has_disk_full_error(
    result: CommandResult | None = None, text: str = ""
) -> bool:
    if result is not None and (
        is_disk_full_output(result.stderr) or is_disk_full_output(result.stdout)
    ):
        return True
    return bool(text and is_disk_full_output(text))


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrashInfo:
    stacktrace: str


@dataclass(frozen=True)
class MiscompilationInfo:
    alive2_output: str


async def check_crash(
    opt_bin: str | Path,
    ir_content: str,
    opt_args: list[str],
    *,
    timeout: int = VERIFY_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> CrashInfo | None:
    fd, tmp_path = tempfile.mkstemp(suffix=".ll")
    try:
        with os.fdopen(fd, "w") as wf:
            wf.write(ir_content)
        ir_file = Path(tmp_path)
        env = minimal_execution_env()
        try:
            await run_command(
                [
                    str(opt_bin),
                    "-S",
                    "-o",
                    "/dev/null",
                    str(ir_file),
                    *opt_args,
                ],
                timeout=timeout,
                env=env,
                memory_limit_bytes=memory_limit_bytes,
            )
        except CommandError as exc:
            result = exc.result
            if result.returncode >= 0:
                return None
            if _output_has_disk_full_error(result):
                return None
            stacktrace = (
                result.stderr or result.stdout or f"signal {abs(result.returncode)}"
            )
            return CrashInfo(stacktrace=stacktrace)
        except asyncio.TimeoutError:
            return None
        return None
    finally:
        _try_unlink(Path(tmp_path))


async def check_miscompilation(
    opt_bin: str | Path,
    alive_tv: str | Path,
    ir_content: str,
    opt_args: list[str],
    *,
    timeout: int = VERIFY_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> MiscompilationInfo | None:
    env = minimal_execution_env()

    fd, tmp_path = tempfile.mkstemp(suffix=".ll")
    try:
        with os.fdopen(fd, "w") as wf:
            wf.write(ir_content)
        ir_file = Path(tmp_path)
        tgt = ir_file.with_suffix(".alive-check.tgt.ll")
        try:
            try:
                await run_command(
                    [
                        str(opt_bin),
                        "-S",
                        "-o",
                        str(tgt),
                        str(ir_file),
                        *opt_args,
                    ],
                    timeout=timeout,
                    env=env,
                    memory_limit_bytes=memory_limit_bytes,
                )
            except (CommandError, asyncio.TimeoutError):
                return None

            try:
                alive_result = await run_command(
                    [
                        str(alive_tv),
                        "--smt-to=10000",
                        "--disable-undef-input",
                        str(ir_file),
                        str(tgt),
                    ],
                    timeout=timeout,
                    check=False,
                )
            except asyncio.TimeoutError:
                return None

            stdout = alive_result.stdout
            if _output_has_disk_full_error(text=stdout):
                return None
            correct = (
                "0 incorrect transformations" in stdout
                and "Transformation seems to be correct" in stdout
            )
            if not correct and ALIVE2_INCORRECT_RE.search(stdout):
                return MiscompilationInfo(alive2_output=stdout)
            return None
        finally:
            _try_unlink(tgt)
    finally:
        _try_unlink(Path(tmp_path))


def _try_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _validate_ir_forbidden_flags(ir_content: str) -> str | None:
    m = _FORBIDDEN_FASTMATH_RE.search(ir_content)
    if m:
        return (
            f"IR contains forbidden fast-math flag '{m.group(1)}'"
            " — only nnan/ninf allowed"
        )
    return None


def _validate_ir_no_undef(ir_content: str) -> str | None:
    if " undef" in ir_content:
        return "IR contains ' undef' — undef values are not allowed in submissions"
    return None


def is_alive2_approximation(info: MiscompilationInfo | None) -> bool:
    if info is None:
        return False
    return "Alive2 approximated the semantics of the programs" in info.alive2_output


async def verify_reproducer(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
    opt_args: list[str],
    *,
    memory_limit_bytes: int | None = None,
) -> tuple[Reproducer | None, str]:
    ir_content = reproducer.source_content
    if ir_content is None:
        reason = "Reproducer has no source content, cannot verify"
        LOGGER.warning(reason)
        return None, reason

    if reproducer.kind == BugKind.CRASH:
        return await _verify_regression_crash(
            reproducer,
            ir_content,
            toolchain,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
    if reproducer.kind == BugKind.MISCOMPILATION:
        return await _verify_regression_miscompilation(
            reproducer,
            ir_content,
            toolchain,
            opt_args,
            memory_limit_bytes=memory_limit_bytes,
        )
    return None, f"Unknown bug kind: {reproducer.kind}"


async def _verify_regression_crash(
    reproducer: Reproducer,
    ir_content: str,
    toolchain: ToolchainPaths,
    opt_args: list[str],
    *,
    memory_limit_bytes: int | None = None,
) -> tuple[Reproducer | None, str]:
    reject = _validate_ir_forbidden_flags(ir_content)
    if reject:
        reason = f"IR contains forbidden fast-math flags: {reject}"
        LOGGER.warning(reason)
        return None, reason

    reject = _validate_ir_no_undef(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    baseline_crash = await check_crash(
        toolchain.baseline_opt,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if baseline_crash is not None:
        reason = "Baseline opt also crashes — not a PR regression"
        LOGGER.warning(reason)
        return None, reason

    pr_crash = await check_crash(
        toolchain.pr_opt,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if pr_crash is None:
        reason = "PR opt did not crash during re-verification"
        LOGGER.warning(reason)
        return None, reason

    LOGGER.info("Verified crash reproducer")
    return Reproducer(
        kind=BugKind.CRASH,
        source_path=reproducer.source_path,
        command=reproducer.command,
        baseline_revision=reproducer.baseline_revision,
        pr_head_sha=reproducer.pr_head_sha,
        patch_sha256=reproducer.patch_sha256,
        stacktrace=pr_crash.stacktrace,
        source_content=ir_content,
    ), ""


async def _verify_regression_miscompilation(
    reproducer: Reproducer,
    ir_content: str,
    toolchain: ToolchainPaths,
    opt_args: list[str],
    *,
    memory_limit_bytes: int | None = None,
) -> tuple[Reproducer | None, str]:
    reject = _validate_ir_forbidden_flags(ir_content)
    if reject:
        reason = f"IR contains forbidden fast-math flags: {reject}"
        LOGGER.warning(reason)
        return None, reason

    reject = _validate_ir_no_undef(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    baseline_mis = await check_miscompilation(
        toolchain.baseline_opt,
        toolchain.alive_tv,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if baseline_mis is not None:
        reason = "Baseline also has Alive2 issues — not a PR regression"
        LOGGER.warning(reason)
        return None, reason

    pr_mis = await check_miscompilation(
        toolchain.pr_opt,
        toolchain.alive_tv,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if pr_mis is None or is_alive2_approximation(pr_mis):
        if pr_mis is not None:
            reason = "Alive2 approximation — not a confirmed miscompilation"
        else:
            reason = "PR opt did not produce incorrect Alive2 result"
        LOGGER.warning(reason)
        return None, reason

    LOGGER.info("Verified miscompilation reproducer")
    return Reproducer(
        kind=BugKind.MISCOMPILATION,
        source_path=reproducer.source_path,
        command=reproducer.command,
        baseline_revision=reproducer.baseline_revision,
        pr_head_sha=reproducer.pr_head_sha,
        patch_sha256=reproducer.patch_sha256,
        alive2_counterexample=pr_mis.alive2_output,
        source_content=ir_content,
    ), ""
