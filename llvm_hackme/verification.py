from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import CommandError, minimal_execution_env, run_command
from llvm_hackme.models import BugKind, Reproducer

LOGGER = logging.getLogger(__name__)

VERIFY_TIMEOUT = 120


class VerificationError(RuntimeError):
    pass


async def verify_reproducer(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    if reproducer.kind == BugKind.CRASH:
        return await _verify_crash(reproducer, toolchain)
    if reproducer.kind == BugKind.MISCOMPILATION:
        return await _verify_miscompilation(reproducer, toolchain)
    return None


async def _verify_crash(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    src = reproducer.source_path
    if not src.exists():
        LOGGER.warning("Crash reproducer source not found: %s", src)
        return None

    tgt_baseline = Path(str(src).replace(".src.ll", ".baseline.tgt.ll"))
    min_env = minimal_execution_env()

    cmd_opt_s = [
        "-S",
        "-o",
        str(tgt_baseline),
        str(src),
        "-passes=instcombine<no-verify-fixpoint>",
    ]

    try:
        await run_command(
            [toolchain.baseline_opt] + cmd_opt_s,
            timeout=VERIFY_TIMEOUT,
            env=min_env,
        )
    except CommandError:
        LOGGER.warning("Baseline opt also crashes on %s — not a PR regression", src)
        return None
    except asyncio.TimeoutError:
        LOGGER.warning("Baseline opt timed out on %s", src)
        return None

    try:
        await run_command(
            [toolchain.pr_opt] + cmd_opt_s,
            timeout=VERIFY_TIMEOUT,
            env=min_env,
        )
    except CommandError as exc:
        result = exc.result
        stacktrace = (
            result.stderr or result.stdout or f"signal {abs(result.returncode)}"
        )
        LOGGER.info("Verified crash reproducer: %s", src)
        return Reproducer(
            kind=BugKind.CRASH,
            source_path=src,
            command=reproducer.command,
            baseline_revision=reproducer.baseline_revision,
            pr_head_sha=reproducer.pr_head_sha,
            patch_sha256=reproducer.patch_sha256,
            stacktrace=stacktrace,
        )
    except asyncio.TimeoutError:
        LOGGER.warning("PR opt timed out on %s during verification", src)
        return None

    LOGGER.warning("PR opt did not crash during re-verification of %s", src)
    return None


async def _verify_miscompilation(
    reproducer: Reproducer,
    toolchain: ToolchainPaths,
) -> Reproducer | None:
    src = reproducer.source_path
    if not src.exists():
        LOGGER.warning("Miscompilation reproducer source not found: %s", src)
        return None

    tgt_baseline = Path(str(src).replace(".src.ll", ".baseline.tgt.ll"))
    tgt_pr = Path(str(src).replace(".src.ll", ".verify.tgt.ll"))
    min_env = minimal_execution_env()

    try:
        await run_command(
            [
                toolchain.baseline_opt,
                "-S",
                "-o",
                tgt_baseline,
                src,
                "-passes=instcombine<no-verify-fixpoint>",
            ],
            timeout=VERIFY_TIMEOUT,
            env=min_env,
        )
    except CommandError:
        LOGGER.warning("Baseline opt failed on miscompilation source %s", src)
        return None
    except asyncio.TimeoutError:
        LOGGER.warning("Baseline opt timed out on %s", src)
        return None

    alive_baseline = await run_command(
        [
            toolchain.alive_tv,
            "--smt-to=200",
            "--disable-undef-input",
            src,
            tgt_baseline,
        ],
        timeout=VERIFY_TIMEOUT,
        check=False,
    )
    if "0 incorrect transformations" not in alive_baseline.stdout:
        LOGGER.warning(
            "Baseline also has Alive2 issues on %s — not a PR regression", src
        )
        return None

    try:
        await run_command(
            [
                toolchain.pr_opt,
                "-S",
                "-o",
                tgt_pr,
                src,
                "-passes=instcombine<no-verify-fixpoint>",
            ],
            timeout=VERIFY_TIMEOUT,
            env=min_env,
        )
    except CommandError:
        LOGGER.warning(
            "PR opt crashed on %s during miscompilation re-verification", src
        )
        return None
    except asyncio.TimeoutError:
        LOGGER.warning("PR opt timed out on %s during re-verification", src)
        return None

    alive_pr = await run_command(
        [
            toolchain.alive_tv,
            "--smt-to=200",
            "--disable-undef-input",
            src,
            tgt_pr,
        ],
        timeout=VERIFY_TIMEOUT,
        check=False,
    )

    if "0 incorrect transformations" not in alive_pr.stdout and (
        "Transformation seems to be correct" in alive_pr.stdout
        or "incorrect" in alive_pr.stdout.lower()
    ):
        LOGGER.info("Verified miscompilation reproducer: %s", src)
        return Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=src,
            command=reproducer.command,
            baseline_revision=reproducer.baseline_revision,
            pr_head_sha=reproducer.pr_head_sha,
            patch_sha256=reproducer.patch_sha256,
            alive2_counterexample=alive_pr.stdout,
        )

    LOGGER.warning("PR Alive2 passed during re-verification of %s", src)
    return None
