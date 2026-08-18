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
    alive2_args: str = ""
    opt_output: str = ""


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
    alive2_extra_args: list[str] | None = None,
) -> MiscompilationInfo | None:
    env = minimal_execution_env()

    if alive2_extra_args is None:
        alive2_extra_args = []
    _validate_alive2_unroll(alive2_extra_args)

    fd, tmp_path = tempfile.mkstemp(suffix=".ll")
    try:
        with os.fdopen(fd, "w") as wf:
            wf.write(ir_content)
        ir_file = Path(tmp_path)
        tgt = ir_file.with_suffix(".alive-check.tgt.ll")
        opt_output = ""
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
                if tgt.exists():
                    with open(tgt, encoding="utf-8") as rf:  # noqa: ASYNC230
                        opt_output = rf.read()
            except (CommandError, asyncio.TimeoutError):
                return None

            # Strip intrinsic declares so alive2 rejects unrecognised
            # intrinsics instead of treating them as ordinary functions.
            with open(ir_file, encoding="utf-8") as rf:  # noqa: ASYNC230
                ir_stripped = _strip_intrinsic_declares(rf.read())
            with open(ir_file, "w", encoding="utf-8") as wf:  # noqa: ASYNC230
                wf.write(ir_stripped)
            if tgt.exists():
                with open(tgt, encoding="utf-8") as rf:  # noqa: ASYNC230
                    tgt_stripped = _strip_intrinsic_declares(rf.read())
                with open(tgt, "w", encoding="utf-8") as wf:  # noqa: ASYNC230
                    wf.write(tgt_stripped)

            try:
                alive_result = await run_command(
                    [
                        str(alive_tv),
                        # High smt-to for thorough verification (fuzzer uses
                        # 100 for speed).
                        "--smt-to=10000",
                        "--disable-undef-input",
                        *alive2_extra_args,
                        str(ir_file),
                        str(tgt),
                    ],
                    timeout=timeout,
                    check=False,
                )
            except asyncio.TimeoutError:
                return None

            stdout = alive_result.stdout
            if _output_has_disk_full_error(result=alive_result, text=stdout):
                return None
            correct = (
                "0 incorrect transformations" in stdout
                and "Transformation seems to be correct" in stdout
            )
            alive2_args_str = " ".join(alive2_extra_args) if alive2_extra_args else ""
            if not correct and ALIVE2_INCORRECT_RE.search(stdout):
                return MiscompilationInfo(
                    alive2_output=stdout,
                    alive2_args=alive2_args_str,
                    opt_output=opt_output,
                )
            return None
        finally:
            _try_unlink(tgt)
    finally:
        _try_unlink(Path(tmp_path))


def _try_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


_MAX_ALIVE2_UNROLL = 128
_ALIVE2_UNROLL_RE = re.compile(r"^-(?:src|tgt)-unroll=(\d+)$")


def _validate_alive2_unroll(args: list[str]) -> None:
    for arg in args:
        m = _ALIVE2_UNROLL_RE.match(arg)
        if m:
            n = int(m.group(1))
            if n > _MAX_ALIVE2_UNROLL:
                raise VerificationError(
                    f"alive2 unroll {n} exceeds maximum {_MAX_ALIVE2_UNROLL}"
                )
            if n < 1:
                raise VerificationError(f"alive2 unroll {n} must be >= 1")


def _validate_ir_forbidden_flags(ir_content: str) -> str | None:
    m = _FORBIDDEN_FASTMATH_RE.search(ir_content)
    if m:
        return (
            f"IR contains forbidden fast-math flag '{m.group(1)}'"
            " — only nnan/ninf allowed"
        )
    return None


_DEFINE_RE = re.compile(r"^\s*define\b", re.MULTILINE)


def _validate_ir_single_function(ir_content: str) -> str | None:
    count = len(_DEFINE_RE.findall(ir_content))
    if count == 0:
        return "No function definition found in IR — at least one define is required"
    if count > 1:
        return (
            f"Found {count} function definitions — only 1 define is allowed. "
            "Extract a single function."
        )
    return None


def _validate_ir_no_undef(ir_content: str) -> str | None:
    if " undef" in ir_content:
        return "IR contains ' undef' — undef values are not allowed in submissions"
    return None


_VSCALE_RE = re.compile(r"\bvscale\b")
_TARGET_DATALAYOUT_RE = re.compile(r'^\s*target\s+datalayout\s*=\s*"', re.MULTILINE)
_TARGET_TRIPLE_RE = re.compile(r'^\s*target\s+triple\s*=\s*"([^"]*)"', re.MULTILINE)


def _validate_ir_vscale_target(ir_content: str, opt_args: list[str]) -> str | None:
    """Scalable-vector (`vscale`) IR needs an explicit aarch64/riscv64 target
    and the matching `-mattr` so opt actually enables the scalable ISA.
    """
    has_vscale = any(
        not line.lstrip().startswith(";") and _VSCALE_RE.search(line)
        for line in ir_content.split("\n")
    )
    if not has_vscale:
        return None
    if _TARGET_DATALAYOUT_RE.search(ir_content) is None:
        return (
            "IR uses 'vscale' but has no target datalayout — must specify "
            'target datalayout = "..." and target triple = "aarch64-..." '
            'or "riscv64-..."'
        )
    triple_m = _TARGET_TRIPLE_RE.search(ir_content)
    if triple_m is None:
        return (
            "IR uses 'vscale' but has no target triple — target triple must "
            'be "aarch64-..." or "riscv64-..."'
        )
    triple = triple_m.group(1)
    if "aarch64" in triple:
        if "-mattr=sve2" not in opt_args:
            return (
                "IR uses 'vscale' with an aarch64 target — opt_args must "
                "include '-mattr=sve2'"
            )
        return None
    if "riscv64" in triple:
        if "-mattr=+v" not in opt_args:
            return (
                "IR uses 'vscale' with a riscv64 target — opt_args must "
                "include '-mattr=+v'"
            )
        return None
    return (
        f"IR uses 'vscale' but target triple {triple!r} is neither aarch64 "
        "nor riscv64 — set target triple to 'aarch64-...' or 'riscv64-...'"
    )


_LLVM_INTRINSIC_RE = re.compile(r"@llvm\.[-\w.$]*")


def _validate_ir_llvm_intrinsics(ir_content: str) -> str | None:
    """`@llvm.*` names may only appear in `declare` statements or as call
    callees — never as globals, constants, or data operands.
    """
    for line_no, line in enumerate(ir_content.split("\n"), start=1):
        stripped = line.lstrip()
        if stripped.startswith(";") or stripped.startswith("declare"):
            continue
        if stripped.startswith("define") and _LLVM_INTRINSIC_RE.search(line):
            return (
                f"Line {line_no}: defining a function named @llvm.* is forbidden — "
                "intrinsics can only be declared or called"
            )
        for m in _LLVM_INTRINSIC_RE.finditer(line):
            if line[m.end() : m.end() + 1] != "(":
                return (
                    f"Line {line_no}: @llvm.* may only be used as a declared "
                    f"intrinsic or call callee, not as a global/constant "
                    f"('{m.group(0)}')"
                )
    return None


def _strip_intrinsic_declares(ir_content: str) -> str:
    """Remove `declare ... @llvm.*` lines so alive2 rejects unrecognised
    intrinsics instead of silently treating them as regular functions.
    """
    return "\n".join(
        line
        for line in ir_content.split("\n")
        if not (line.lstrip().startswith("declare ") and "@llvm." in line)
    )


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
    alive2_extra_args: list[str] | None = None,
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
            alive2_extra_args=alive2_extra_args,
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
        LOGGER.warning(reject)
        return None, reject
    reject = _validate_ir_llvm_intrinsics(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject
    reject = _validate_ir_vscale_target(ir_content, opt_args)
    if reject:
        LOGGER.warning(reject)
        return None, reject
    # We intentionally allow 'undef' in crash IR — undef values can
    # trigger UB paths that lead to legitimate crashes.  This is
    # different from miscompilation verification, where 'undef' would
    # poison the Alive2 comparison.

    baseline_crash = await check_crash(
        toolchain.baseline_opt,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if baseline_crash is not None:
        last_lines = baseline_crash.stacktrace.strip().split("\n")[-3:]
        tail = " | ".join(last_lines)[:400]
        reason = (
            "Baseline opt also crashes — not a PR regression. "
            "Baseline crash output tail: " + tail
        )
        LOGGER.warning(reason)
        return None, reason

    pr_crash = await check_crash(
        toolchain.pr_opt,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
    )
    if pr_crash is None:
        # Check if IR is even valid by running opt on it directly
        verify_result = await check_crash(
            toolchain.pr_opt,
            ir_content,
            [],
            memory_limit_bytes=memory_limit_bytes,
        )
        if verify_result is not None:
            last_lines = verify_result.stacktrace.strip().split("\n")[-5:]
            tail = " | ".join(last_lines)[:500]
            reason = (
                "PR opt did not crash — your IR is not valid LLVM. "
                "Fix the IR.  Verifier error: " + tail
            )
        else:
            reason = (
                "PR opt did not crash — opt exited normally. "
                "The modified code was either not reached or handled your IR "
                "safely.  Try different IR that exercises the modified code path."
            )
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
        source=reproducer.source,
        stacktrace=pr_crash.stacktrace,
        source_content=_strip_intrinsic_declares(ir_content),
    ), ""


async def _verify_regression_miscompilation(
    reproducer: Reproducer,
    ir_content: str,
    toolchain: ToolchainPaths,
    opt_args: list[str],
    *,
    memory_limit_bytes: int | None = None,
    alive2_extra_args: list[str] | None = None,
) -> tuple[Reproducer | None, str]:
    reject = _validate_ir_forbidden_flags(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    reject = _validate_ir_llvm_intrinsics(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    reject = _validate_ir_no_undef(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    reject = _validate_ir_single_function(ir_content)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    reject = _validate_ir_vscale_target(ir_content, opt_args)
    if reject:
        LOGGER.warning(reject)
        return None, reject

    baseline_mis = await check_miscompilation(
        toolchain.baseline_opt,
        toolchain.alive_tv,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
        alive2_extra_args=alive2_extra_args,
    )
    if baseline_mis is not None:
        last_lines = baseline_mis.alive2_output.strip().split("\n")[:5]
        tail = " | ".join(last_lines)[:400]
        reason = (
            "Baseline already produces Alive2 issues — not a PR regression. "
            "Baseline alive2 output: " + tail
        )
        LOGGER.warning(reason)
        return None, reason

    pr_mis = await check_miscompilation(
        toolchain.pr_opt,
        toolchain.alive_tv,
        ir_content,
        opt_args,
        memory_limit_bytes=memory_limit_bytes,
        alive2_extra_args=alive2_extra_args,
    )
    if pr_mis is None or is_alive2_approximation(pr_mis):
        if pr_mis is not None:
            reason = (
                "Alive2 approximation — alive2 could not fully verify the outputs, "
                "so this is not a confirmed miscompilation.  Simplify your IR."
            )
        else:
            # Check if IR is valid
            verify_result = await check_crash(
                toolchain.pr_opt,
                ir_content,
                [],
                memory_limit_bytes=memory_limit_bytes,
            )
            if verify_result is not None:
                last_lines = verify_result.stacktrace.strip().split("\n")[-5:]
                tail = " | ".join(last_lines)[:500]
                reason = (
                    "PR opt did not produce incorrect Alive2 result — your IR is "
                    "not valid LLVM.  Fix the IR.  Error: " + tail
                )
            else:
                reason = (
                    "PR opt did not produce incorrect Alive2 result — "
                    "baseline and PR produced equivalent outputs. "
                    "The transform is either correct or your IR does not exercise "
                    "it.  Try different IR or check the counterexample values."
                )
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
        source=reproducer.source,
        alive2_counterexample=pr_mis.alive2_output,
        alive2_args=pr_mis.alive2_args,
        opt_output=pr_mis.opt_output,
        source_content=_strip_intrinsic_declares(ir_content),
    ), ""
