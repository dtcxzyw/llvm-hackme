from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import (
    CommandError,
    minimal_execution_env,
    run_command,
)
from llvm_hackme.config import Config
from llvm_hackme.models import BugKind, Reproducer
from llvm_hackme.passes import guess_pass_name

LOGGER = logging.getLogger(__name__)

FUNC_RE = re.compile(r"define [^@]+@([-\w]+)\(")
_ALIVE2_INCORRECT_RE = re.compile(
    r"[1-9]\d* incorrect transformations?|ERROR: Value mismatch"
)
_DISK_FULL_RE = re.compile(r"No space left on device|ENOSPC")

_MUTATE_TIMEOUT = 30
_OPT_TIMEOUT = 60
_ALIVE2_TIMEOUT = 60
_SMT_TO = 100
_JITTER_US = 10_000


def _read_content(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _run_sync(
    cmd: list[str],
    *,
    timeout: int,
    env: dict,
    mem_limit: int,
) -> subprocess.CompletedProcess:
    limit_cmd: list[str] = []
    if mem_limit:
        import shutil as _shutil

        prlimit = _shutil.which("prlimit")
        if prlimit:
            limit_cmd = [prlimit, f"--as={mem_limit}"]
    return subprocess.run(
        limit_cmd + cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _is_disk_full(text: str) -> bool:
    return bool(_DISK_FULL_RE.search(text))


@dataclass(frozen=True)
class FuzzResult:
    reproducer: Reproducer | None
    mutation_count: int = 0


class FuzzRunner:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._opt_memory_limit = config.opt_memory_limit_bytes
        self._fuzz_budget = config.fuzz_budget_seconds

    async def run(
        self,
        patch: str,
        patch_sha256: str,
        pr_head_sha: str,
        toolchain: ToolchainPaths,
    ) -> FuzzResult:
        pass_name = guess_pass_name(patch)
        if pass_name is None:
            LOGGER.warning("Could not guess pass name from patch")
            return FuzzResult(reproducer=None, mutation_count=0)
        LOGGER.info("Guessed pass name from patch: %s", pass_name)

        work = self._config.fuzz_work_dir
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)

        seeds = self._collect_seeds(patch)
        if not seeds:
            LOGGER.info("No seed functions found in patch")
            return FuzzResult(reproducer=None, mutation_count=0)

        seeds_dir = work / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)

        if not await self._extract_seeds(seeds, seeds_dir, toolchain):
            LOGGER.info("Failed to extract seeds")
            return FuzzResult(reproducer=None, mutation_count=0)

        if not any(seeds_dir.iterdir()):
            LOGGER.info("No seed files extracted")
            return FuzzResult(reproducer=None, mutation_count=0)

        seeds_file = work / "seeds.ll"
        try:
            await run_command(
                [toolchain.merge, seeds_dir, seeds_file],
                timeout=30,
            )
        except CommandError:
            LOGGER.exception("merge failed")
            return FuzzResult(reproducer=None, mutation_count=0)

        try:
            await run_command(
                [
                    toolchain.baseline_opt,
                    "-S",
                    "-o",
                    "/dev/null",
                    seeds_file,
                    f"-passes={pass_name}",
                ],
                timeout=60,
                env=minimal_execution_env(),
                memory_limit_bytes=self._opt_memory_limit,
            )
        except CommandError:
            LOGGER.exception("baseline opt on seeds failed")
            return FuzzResult(reproducer=None, mutation_count=0)

        return await self._fuzz_loop(
            work,
            seeds_file,
            toolchain,
            patch_sha256,
            pr_head_sha,
            pass_name,
        )

    def _collect_seeds(self, patch: str) -> list[tuple[str, str]]:
        func_re = re.compile(r"define [^@]+@([-\w]+)\(")
        seeds: list[tuple[str, str]] = []
        current_file = ""
        seen: set[tuple[str, str]] = set()

        for line in patch.split("\n"):
            if line.startswith("diff --git a/"):
                current_file = line.removeprefix("diff --git a/").split(" ", 1)[0]
                continue
            if current_file.endswith(".ll"):
                matched = func_re.search(line)
                if matched:
                    func_name = matched.group(1)
                    key = (current_file, func_name)
                    if key not in seen:
                        seen.add(key)
                        seeds.append(key)

        return seeds

    async def _extract_seeds(
        self,
        seeds: list[tuple[str, str]],
        seeds_dir: Path,
        toolchain: ToolchainPaths,
    ) -> bool:
        any_success = False
        for i, (file, func) in enumerate(seeds):
            src = self._config.llvm_project_pr_dir / file
            if not src.exists():
                LOGGER.warning("Seed source file not found: %s", src)
                continue
            try:
                await run_command(
                    [
                        toolchain.llvm_extract,
                        "-S",
                        "-func",
                        func,
                        "-o",
                        seeds_dir / f"seed{i}.ll",
                        src,
                    ],
                    timeout=30,
                )
            except CommandError:
                LOGGER.warning("llvm-extract failed for %s @%s", file, func)
            else:
                any_success = True
        return any_success

    async def _fuzz_loop(
        self,
        work: Path,
        seeds_file: Path,
        toolchain: ToolchainPaths,
        patch_sha256: str,
        pr_head_sha: str,
        pass_name: str,
    ) -> FuzzResult:
        processes = os.cpu_count()
        files_per_batch = 4 * processes
        deadline = time.monotonic() + self._fuzz_budget
        idx = 0

        ctx = _WorkerContext(
            work_dir=str(work),
            seeds_file=str(seeds_file),
            mutate_bin=str(toolchain.mutate),
            opt_bin=str(toolchain.pr_opt),
            alive2_bin=str(toolchain.alive_tv),
            llvm_extract_bin=str(toolchain.llvm_extract),
            llvm_reduce_bin=str(toolchain.llvm_reduce),
            pass_name=pass_name,
            baseline_revision=toolchain.baseline_revision,
            pr_head_sha=pr_head_sha,
            patch_sha256=patch_sha256,
            opt_memory_bytes=self._opt_memory_limit,
        )

        result: Reproducer | None = None

        with ProcessPoolExecutor(max_workers=processes) as pool:
            while time.monotonic() < deadline and result is None:
                batch_end = idx + files_per_batch
                futures: dict[Future, int] = {}
                for i in range(idx, batch_end):
                    fut = pool.submit(_run_fuzz_iteration, ctx, i)
                    futures[fut] = i
                    _jitter()

                idx = batch_end

                for fut in as_completed(futures):
                    attempt = futures[fut]
                    try:
                        reproducer = fut.result()
                    except Exception:
                        LOGGER.exception("Fuzz iteration %s failed", attempt)
                        continue
                    if reproducer is not None:
                        result = reproducer
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

                if time.monotonic() >= deadline:
                    break

        if result is not None:
            return FuzzResult(reproducer=result, mutation_count=idx)

        return FuzzResult(reproducer=None, mutation_count=idx)


@dataclass(frozen=True)
class _WorkerContext:
    work_dir: str
    seeds_file: str
    mutate_bin: str
    opt_bin: str
    alive2_bin: str
    llvm_extract_bin: str
    llvm_reduce_bin: str
    pass_name: str
    baseline_revision: str
    pr_head_sha: str
    patch_sha256: str
    opt_memory_bytes: int


def _run_fuzz_iteration(ctx: _WorkerContext, idx: int) -> Reproducer | None:
    work = Path(ctx.work_dir)
    seeds_file = Path(ctx.seeds_file)
    src_file = work / f"correctness-{idx}.src.ll"
    tgt_file = work / f"correctness-{idx}.tgt.ll"
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    try:
        _run_sync(
            [ctx.mutate_bin, str(seeds_file), str(src_file)],
            timeout=_MUTATE_TIMEOUT,
            env=env,
            mem_limit=0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    try:
        proc = _run_sync(
            [
                ctx.opt_bin,
                "-S",
                "-o",
                str(tgt_file),
                str(src_file),
                f"-passes={ctx.pass_name}",
            ],
            timeout=_OPT_TIMEOUT,
            env=env,
            mem_limit=ctx.opt_memory_bytes,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None

    if proc.returncode is not None and proc.returncode < 0:
        stderr_out = proc.stderr or ""
        if _is_disk_full(stderr_out):
            return None
        source_path = _reduce_crash(ctx, idx, src_file, work) or src_file
        stacktrace = stderr_out or f"signal {abs(proc.returncode)}"
        return Reproducer(
            kind=BugKind.CRASH,
            source_path=source_path,
            command=[
                ctx.opt_bin,
                "-S",
                "-o",
                str(tgt_file),
                str(source_path),
                f"-passes={ctx.pass_name}",
            ],
            baseline_revision=ctx.baseline_revision,
            pr_head_sha=ctx.pr_head_sha,
            patch_sha256=ctx.patch_sha256,
            stacktrace=stacktrace,
            source_content=_read_content(source_path),
        )

    if proc.returncode != 0:
        return None

    try:
        proc = _run_sync(
            [
                ctx.alive2_bin,
                "--smt-to=100",
                "--disable-undef-input",
                str(src_file),
                str(tgt_file),
            ],
            timeout=_ALIVE2_TIMEOUT,
            env=env,
            mem_limit=ctx.opt_memory_bytes,
        )
    except subprocess.TimeoutExpired:
        return None
    except (subprocess.CalledProcessError, OSError):
        return None

    combined = proc.stdout + proc.stderr
    correct = (
        "0 incorrect transformations" in combined
        and "Transformation seems to be correct" in combined
    )
    if not correct and _ALIVE2_INCORRECT_RE.search(combined):
        if _is_disk_full(combined):
            return None
        source_path = src_file
        func_re = re.compile(r"define [^@]+@([-\w]+)\(")
        func_match = func_re.search(src_file.read_text(errors="replace"))
        if func_match:
            extracted = work / f"correctness-{idx}.extracted.ll"
            try:
                subprocess.run(
                    [
                        ctx.llvm_extract_bin,
                        "-S",
                        "-func",
                        func_match.group(1),
                        "-o",
                        str(extracted),
                        str(src_file),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if extracted.exists() and extracted.stat().st_size > 0:
                    source_path = extracted
            except (subprocess.TimeoutExpired, OSError):
                pass
        return Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=source_path,
            command=[
                ctx.opt_bin,
                "-S",
                "-o",
                str(tgt_file),
                str(source_path),
                f"-passes={ctx.pass_name}",
            ],
            baseline_revision=ctx.baseline_revision,
            pr_head_sha=ctx.pr_head_sha,
            patch_sha256=ctx.patch_sha256,
            alive2_counterexample=combined,
            source_content=_read_content(source_path),
        )

    return None


def _reduce_crash(
    ctx: _WorkerContext,
    idx: int,
    src_file: Path,
    work: Path,
) -> Path | None:
    test_script = work / f"interestingness-{idx}.sh"
    test_script.write_text(
        "#!/bin/bash\n"
        f"{shlex.quote(ctx.opt_bin)} -S -o /dev/null"
        f" -passes={shlex.quote(ctx.pass_name)}"
        f' "$1" >/dev/null 2>&1\n'
        "test $? -ne 0\n",
    )
    test_script.chmod(0o755)
    reduced = work / f"correctness-{idx}.reduced.ll"
    try:
        subprocess.run(
            [
                ctx.llvm_reduce_bin,
                f"--test={test_script}",
                str(src_file),
                "-o",
                str(reduced),
            ],
            capture_output=True,
            timeout=120,
            env={
                "HOME": os.environ.get("HOME", "/tmp"),
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )
        if reduced.exists():
            return reduced
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _jitter() -> None:
    import random

    time.sleep(random.randint(0, _JITTER_US) / 1_000_000)
