from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import re
import resource
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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


_command_log_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_command_log_path", default=None
)


def set_command_log_path(path: Path | None) -> None:
    _command_log_path.set(path)


def get_command_log_path() -> Path | None:
    return _command_log_path.get(None)


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
    _write_command_log(normalized, result)
    if check and result.returncode != 0:
        raise CommandError(result)
    return result


def _write_command_log(args: tuple[str, ...], result: CommandResult) -> None:
    log_path = _command_log_path.get(None)
    if log_path is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(log_path, "a") as f:
            f.write(f"[{ts}] {' '.join(args)}\n")
            if result.stdout:
                f.write(result.stdout)
                if not result.stdout.endswith("\n"):
                    f.write("\n")
            if result.stderr:
                f.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    f.write("\n")
    except OSError:
        pass


def append_command_log_message(message: str) -> None:
    log_path = _command_log_path.get(None)
    if log_path is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(log_path, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


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
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(
                resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes)
            )

    return apply_limit


_DISK_FULL_RE = re.compile(
    r"(?i)("
    r"no space left on device|"
    r"disk quota exceeded|"
    r"not enough space|"
    r"disk full|"
    r"errno\s*28"
    r")"
)


def is_disk_full_output(text: str) -> bool:
    return bool(_DISK_FULL_RE.search(text))


_TRANSIENT_KEYWORDS = (
    "connection refused",
    "connection reset",
    "connection timed out",
    "name or service not known",
    "network is unreachable",
    "temporary failure",
    "could not resolve host",
    "connection closed",
    "broken pipe",
    "socket error",
    "service unavailable",
    "remote end closed",
    "no route to host",
)


def find_opencode() -> str | None:
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


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    exc_type = type(exc).__name__
    network_types = {
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "PoolTimeout",
        "APIConnectionError",
        "APITimeoutError",
        "ConnectionError",
        "TimeoutError",
        "Timeout",
    }
    if exc_type in network_types:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _TRANSIENT_KEYWORDS)
