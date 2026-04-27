from __future__ import annotations

import asyncio
import contextlib
import os
import resource
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(
            f"Command failed with exit code {result.returncode}:"
            f" {' '.join(result.args)}"
        )
        self.result = result


async def run_command(
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
    memory_limit_bytes: int | None = None,
) -> CommandResult:
    normalized = tuple(str(arg) for arg in args)
    process_env = dict(os.environ)
    if env is not None:
        process_env.update(env)
    preexec_fn = None
    if memory_limit_bytes is not None:
        preexec_fn = _limit_address_space(memory_limit_bytes)
    process = await asyncio.create_subprocess_exec(
        *normalized,
        cwd=str(cwd) if cwd is not None else None,
        env=process_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec_fn,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)
        raise
    result = CommandResult(
        args=normalized,
        returncode=process.returncode,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
    )
    if check and result.returncode != 0:
        raise CommandError(result)
    return result


def minimal_execution_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    if extra is not None:
        env.update(extra)
    return env


def _limit_address_space(memory_limit_bytes: int):
    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))

    return apply_limit
