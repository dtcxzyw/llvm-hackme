from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import (
    CommandError,
    is_disk_full_output,
    minimal_execution_env,
    run_command,
)
from llvm_hackme.config import Config
from llvm_hackme.models import BugKind, Reproducer
from llvm_hackme.passes import guess_pass_name

LOGGER = logging.getLogger(__name__)

FUNC_RE = re.compile(r"define [^@]+@([-\w]+)\(")
ALIVE2_INCORRECT_RE = re.compile(
    r"[1-9]\d* incorrect transformations?|ERROR: Value mismatch"
)


def _is_safe_subpath(path: str, *, base_dir: Path) -> bool:
    if not path or path.startswith("/") or path.startswith(".."):
        return False
    if os.pardir in path.split("/"):
        return False
    resolved = (base_dir / path).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError:
        return False
    return True


def _read_content(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


@dataclass(frozen=True)
class FuzzResult:
    reproducer: Reproducer | None
    mutation_count: int = 0


class FuzzRunner:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._opt_memory_limit = config.opt_memory_limit_bytes
        self._fuzz_budget = config.fuzz_budget_seconds
        self._max_parallelism = config.max_fuzz_parallelism

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
            return FuzzResult()
        LOGGER.info("Guessed pass name from patch: %s", pass_name)

        work = self._config.fuzz_work_dir
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)

        seeds = self._collect_seeds(patch)
        if not seeds:
            LOGGER.info("No seed functions found in patch")
            return FuzzResult()

        seeds_dir = work / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)

        if not await self._extract_seeds(seeds, seeds_dir, toolchain):
            LOGGER.info("Failed to extract seeds")
            return FuzzResult()

        if not any(seeds_dir.iterdir()):
            LOGGER.info("No seed files extracted")
            return FuzzResult()

        seeds_file = work / "seeds.ll"
        try:
            await run_command(
                [toolchain.merge, seeds_dir, seeds_file],
                timeout=30,
            )
        except CommandError:
            LOGGER.exception("merge failed")
            return FuzzResult()

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
            return FuzzResult()

        return await self._fuzz_loop(
            work,
            seeds_file,
            toolchain,
            patch_sha256,
            pr_head_sha,
            pass_name,
        )

    def _collect_seeds(self, patch: str) -> list[tuple[str, str]]:
        seeds: list[tuple[str, str]] = []
        current_file = ""
        seen: set[tuple[str, str]] = set()

        for line in patch.split("\n"):
            if line.startswith("diff --git a/"):
                current_file = line.removeprefix("diff --git a/").split(" ", 1)[0]
                continue
            if current_file.endswith(".ll"):
                matched = FUNC_RE.search(line)
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
            if not _is_safe_subpath(file, base_dir=self._config.llvm_project_pr_dir):
                LOGGER.warning("Rejecting unsafe seed path: %s", file)
                continue
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
        config = self._config
        deadline = time.monotonic() + self._fuzz_budget
        idx = 0
        found: asyncio.Event = asyncio.Event()
        result_holder: dict[str, Reproducer | None] = {}

        async def one_iteration(idx: int) -> Reproducer | None:
            if found.is_set():
                return None
            try:
                return await self._fuzz_one(
                    work,
                    idx,
                    seeds_file,
                    toolchain,
                    patch_sha256,
                    pr_head_sha,
                    pass_name,
                )
            except Exception:
                LOGGER.exception("Fuzz iteration %s failed", idx)
                return None

        active: set[asyncio.Task[Reproducer | None]] = set()
        try:
            while time.monotonic() < deadline and not found.is_set():
                while len(active) >= config.max_fuzz_parallelism:
                    done, active = await asyncio.wait(
                        active, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in done:
                        result = t.result()
                        if result is not None and not found.is_set():
                            found.set()
                            result_holder["reproducer"] = result

                task = asyncio.create_task(one_iteration(idx))
                active.add(task)
                idx += 1
        finally:
            for t in active:
                if not t.done():
                    t.cancel()

        reproducer = result_holder.get("reproducer")
        if reproducer is not None:
            return FuzzResult(reproducer=reproducer, mutation_count=idx)

        return FuzzResult(reproducer=None, mutation_count=idx)

    async def _fuzz_one(
        self,
        work: Path,
        idx: int,
        seeds_file: Path,
        toolchain: ToolchainPaths,
        patch_sha256: str,
        pr_head_sha: str,
        pass_name: str,
    ) -> Reproducer | None:
        src_file = work / f"correctness-{idx}.src.ll"
        tgt_file = work / f"correctness-{idx}.tgt.ll"

        try:
            await run_command(
                [toolchain.mutate, seeds_file, src_file],
                timeout=30,
            )
        except CommandError:
            return None

        min_env = minimal_execution_env()
        try:
            await run_command(
                [
                    toolchain.pr_opt,
                    "-S",
                    "-o",
                    tgt_file,
                    src_file,
                    f"-passes={pass_name}",
                ],
                timeout=60,
                env=min_env,
                memory_limit_bytes=self._opt_memory_limit,
            )
        except CommandError as exc:
            result = exc.result
            if result.returncode < 0:
                if is_disk_full_output(result.stderr) or is_disk_full_output(
                    result.stdout
                ):
                    return None
                source_path = src_file
                reduced = await self._reduce_crash(
                    src_file, toolchain, idx, work, pass_name
                )
                if reduced is not None:
                    source_path = reduced
                return Reproducer(
                    kind=BugKind.CRASH,
                    source_path=source_path,
                    command=[
                        str(toolchain.pr_opt),
                        "-S",
                        "-o",
                        str(tgt_file),
                        str(source_path),
                        f"-passes={pass_name}",
                    ],
                    baseline_revision=toolchain.baseline_revision,
                    pr_head_sha=pr_head_sha,
                    patch_sha256=patch_sha256,
                    stacktrace=result.stderr
                    or result.stdout
                    or f"signal {abs(result.returncode)}",
                    source_content=_read_content(source_path),
                )
            return None
        except asyncio.TimeoutError:
            return None

        try:
            result = await run_command(
                [
                    toolchain.alive_tv,
                    "--smt-to=100",
                    "--disable-undef-input",
                    src_file,
                    tgt_file,
                ],
                timeout=60,
                check=False,
                env=min_env,
                memory_limit_bytes=self._opt_memory_limit,
            )
        except asyncio.TimeoutError:
            return None

        stdout = result.stdout
        correct = (
            "0 incorrect transformations" in stdout
            and "Transformation seems to be correct" in stdout
        )
        if not correct and ALIVE2_INCORRECT_RE.search(stdout):
            if is_disk_full_output(stdout):
                return None
            source_path = src_file
            func_match = FUNC_RE.search(src_file.read_text())
            if func_match:
                extracted = work / f"correctness-{idx}.extracted.ll"
                try:
                    await run_command(
                        [
                            toolchain.llvm_extract,
                            "-S",
                            "-func",
                            func_match.group(1),
                            "-o",
                            extracted,
                            src_file,
                        ],
                        timeout=30,
                    )
                    if extracted.exists() and extracted.stat().st_size > 0:
                        source_path = extracted
                except CommandError:
                    LOGGER.warning("llvm-extract failed for iteration %s", idx)
            return Reproducer(
                kind=BugKind.MISCOMPILATION,
                source_path=source_path,
                command=[
                    str(toolchain.pr_opt),
                    "-S",
                    "-o",
                    str(tgt_file),
                    str(source_path),
                    f"-passes={pass_name}",
                ],
                baseline_revision=toolchain.baseline_revision,
                pr_head_sha=pr_head_sha,
                patch_sha256=patch_sha256,
                alive2_counterexample=stdout,
                source_content=_read_content(source_path),
            )

        return None

    async def _reduce_crash(
        self,
        src_file: Path,
        toolchain: ToolchainPaths,
        idx: int,
        work: Path,
        pass_name: str,
    ) -> Path | None:
        test_script = work / f"interestingness-{idx}.sh"
        test_script.write_text(
            "#!/bin/bash\n"
            f"{shlex.quote(str(toolchain.pr_opt))} -S -o /dev/null"
            f" -passes={shlex.quote(pass_name)}"
            f' "$1" >/dev/null 2>&1\n'
            "test $? -ne 0\n"
        )
        test_script.chmod(0o755)
        reduced = work / f"correctness-{idx}.reduced.ll"
        try:
            await run_command(
                [
                    toolchain.llvm_reduce,
                    f"--test={test_script}",
                    str(src_file),
                    "-o",
                    str(reduced),
                ],
                timeout=120,
                env=minimal_execution_env(),
            )
            if reduced.exists():
                LOGGER.info("llvm-reduce succeeded for iteration %s", idx)
                return reduced
        except (CommandError, asyncio.TimeoutError):
            LOGGER.warning("llvm-reduce failed for iteration %s", idx)
        return None
