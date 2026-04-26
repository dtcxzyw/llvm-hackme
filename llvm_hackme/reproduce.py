from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp

from llvm_hackme.commands import CommandError, minimal_execution_env, run_command
from llvm_hackme.models import BugKind

PASS_NAME = "instcombine<no-verify-fixpoint>"
DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class CrashResult:
    crashed: bool
    stacktrace: str | None = None


@dataclass(frozen=True)
class MiscompilationResult:
    miscompiled: bool
    alive2_output: str | None = None


async def reproduce_crash(
    opt_bin: str | Path,
    ir_input: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> CrashResult:
    env = minimal_execution_env()
    try:
        await run_command(
            [
                str(opt_bin),
                "-S",
                "-o",
                "/dev/null",
                str(ir_input),
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
        return CrashResult(crashed=True, stacktrace=stacktrace)
    except asyncio.TimeoutError:
        return CrashResult(crashed=False)
    return CrashResult(crashed=False)


async def reproduce_miscompilation(
    opt_bin: str | Path,
    alive_tv: str | Path,
    ir_input: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> MiscompilationResult:
    env = minimal_execution_env()

    fd, tgt_path = mkstemp(suffix=".ll")
    os.close(fd)
    tgt = Path(tgt_path)
    try:
        try:
            await run_command(
                [
                    str(opt_bin),
                    "-S",
                    "-o",
                    str(tgt),
                    str(ir_input),
                    f"-passes={PASS_NAME}",
                ],
                timeout=timeout,
                env=env,
                memory_limit_bytes=memory_limit_bytes,
            )
        except (CommandError, asyncio.TimeoutError):
            return MiscompilationResult(miscompiled=False)

        try:
            alive_result = await run_command(
                [
                    str(alive_tv),
                    "--smt-to=200",
                    "--disable-undef-input",
                    str(ir_input),
                    str(tgt),
                ],
                timeout=timeout,
                check=False,
            )
        except asyncio.TimeoutError:
            return MiscompilationResult(miscompiled=False)

        stdout = alive_result.stdout
        miscompiled = "0 incorrect transformations" not in stdout and (
            "Transformation seems to be correct" in stdout
            or "ERROR" in stdout
            or "incorrect" in stdout.lower()
        )
        return MiscompilationResult(
            miscompiled=miscompiled,
            alive2_output=stdout if miscompiled else None,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tgt_path)


async def reproduce(
    kind: BugKind,
    opt_bin: str | Path,
    ir_input: Path,
    *,
    alive_tv: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    memory_limit_bytes: int | None = None,
) -> CrashResult | MiscompilationResult:
    if kind == BugKind.CRASH:
        return await reproduce_crash(
            opt_bin,
            ir_input,
            timeout=timeout,
            memory_limit_bytes=memory_limit_bytes,
        )
    if kind == BugKind.MISCOMPILATION:
        if alive_tv is None:
            raise ValueError("alive_tv is required for miscompilation reproduction")
        return await reproduce_miscompilation(
            opt_bin,
            alive_tv,
            ir_input,
            timeout=timeout,
            memory_limit_bytes=memory_limit_bytes,
        )
    raise ValueError(f"Unknown bug kind: {kind}")
