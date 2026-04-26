from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import CommandError, minimal_execution_env, run_command
from llvm_hackme.models import BugKind, Reproducer

LOGGER = logging.getLogger(__name__)

VERIFY_TIMEOUT = 120
PASS_NAME = "instcombine<no-verify-fixpoint>"


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
    ir_source: Path,
    *,
    timeout: int = VERIFY_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> CrashInfo | None:
    env = minimal_execution_env()
    try:
        await run_command(
            [
                str(opt_bin),
                "-S",
                "-o",
                "/dev/null",
                str(ir_source),
                f"-passes={PASS_NAME}",
            ],
            timeout=timeout,
            env=env,
            memory_limit_bytes=memory_limit_bytes,
        )
    except CommandError as exc:
        result = exc.result
        stacktrace = (
            result.stderr or result.stdout or f"signal {abs(result.returncode)}"
        )
        return CrashInfo(stacktrace=stacktrace)
    except asyncio.TimeoutError:
        return None
    return None


async def check_miscompilation(
    opt_bin: str | Path,
    alive_tv: str | Path,
    ir_source: Path,
    *,
    timeout: int = VERIFY_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> MiscompilationInfo | None:
    env = minimal_execution_env()

    tgt = ir_source.with_suffix(".alive-check.tgt.ll")
    try:
        try:
            await run_command(
                [
                    str(opt_bin),
                    "-S",
                    "-o",
                    str(tgt),
                    str(ir_source),
                    f"-passes={PASS_NAME}",
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
                    "--smt-to=200",
                    "--disable-undef-input",
                    str(ir_source),
                    str(tgt),
                ],
                timeout=timeout,
                check=False,
            )
        except asyncio.TimeoutError:
            return None

        stdout = alive_result.stdout
        if "0 incorrect transformations" not in stdout and (
            "Transformation seems to be correct" in stdout
            or "ERROR" in stdout
            or "incorrect" in stdout.lower()
        ):
            return MiscompilationInfo(alive2_output=stdout)
        return None
    finally:
        _try_unlink(tgt)


def _try_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


async def verify_reproducer(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    if reproducer.kind == BugKind.CRASH:
        return await _verify_regression_crash(reproducer, toolchain)
    if reproducer.kind == BugKind.MISCOMPILATION:
        return await _verify_regression_miscompilation(reproducer, toolchain)
    return None


async def _verify_regression_crash(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    src = reproducer.source_path

    baseline_crash = await check_crash(toolchain.baseline_opt, src)
    if baseline_crash is not None:
        LOGGER.warning("Baseline opt also crashes on %s — not a PR regression", src)
        return None

    pr_crash = await check_crash(toolchain.pr_opt, src)
    if pr_crash is None:
        LOGGER.warning("PR opt did not crash during re-verification of %s", src)
        return None

    LOGGER.info("Verified crash reproducer: %s", src)
    return Reproducer(
        kind=BugKind.CRASH,
        source_path=src,
        command=reproducer.command,
        baseline_revision=reproducer.baseline_revision,
        pr_head_sha=reproducer.pr_head_sha,
        patch_sha256=reproducer.patch_sha256,
        stacktrace=pr_crash.stacktrace,
    )


async def _verify_regression_miscompilation(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    src = reproducer.source_path

    baseline_mis = await check_miscompilation(
        toolchain.baseline_opt, toolchain.alive_tv, src
    )
    if baseline_mis is not None:
        LOGGER.warning(
            "Baseline also has Alive2 issues on %s — not a PR regression", src
        )
        return None

    pr_mis = await check_miscompilation(toolchain.pr_opt, toolchain.alive_tv, src)
    if pr_mis is None:
        LOGGER.warning("PR Alive2 passed during re-verification of %s", src)
        return None

    LOGGER.info("Verified miscompilation reproducer: %s", src)
    return Reproducer(
        kind=BugKind.MISCOMPILATION,
        source_path=src,
        command=reproducer.command,
        baseline_revision=reproducer.baseline_revision,
        pr_head_sha=reproducer.pr_head_sha,
        patch_sha256=reproducer.patch_sha256,
        alive2_counterexample=pr_mis.alive2_output,
    )
