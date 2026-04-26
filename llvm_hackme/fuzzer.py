from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
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

LOGGER = logging.getLogger(__name__)

PASS_NAME = "instcombine<no-verify-fixpoint>"


def _is_safe_subpath(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith(".."):
        return False
    return os.pardir not in path.split("/")


FUNC_RE = re.compile(r"define [^@]+@([-\w]+)\(")

FUZZ_RECIPE = "correctness"


@dataclass(frozen=True)
class FuzzResult:
    reproducer: Reproducer | None


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
        work = self._config.fuzz_work_dir
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)

        seeds = self._collect_seeds(patch)
        if not seeds:
            LOGGER.info("No seed functions found in patch")
            return FuzzResult(reproducer=None)

        seeds_dir = work / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)

        if not await self._extract_seeds(seeds, seeds_dir, toolchain):
            LOGGER.info("Failed to extract seeds")
            return FuzzResult(reproducer=None)

        if not any(seeds_dir.iterdir()):
            LOGGER.info("No seed files extracted")
            return FuzzResult(reproducer=None)

        seeds_file = work / "seeds.ll"
        seeds_ref_file = work / "seeds_ref.ll"
        try:
            await run_command(
                [toolchain.merge, seeds_dir, seeds_file],
                timeout=30,
            )
        except CommandError:
            LOGGER.exception("merge failed")
            return FuzzResult(reproducer=None)

        try:
            await run_command(
                [
                    toolchain.baseline_opt,
                    "-S",
                    "-o",
                    seeds_ref_file,
                    seeds_file,
                    f"-passes={PASS_NAME}",
                ],
                timeout=60,
                env=minimal_execution_env(),
            )
        except CommandError:
            LOGGER.exception("baseline opt on seeds failed")
            return FuzzResult(reproducer=None)

        return await self._fuzz_loop(
            work,
            seeds_file,
            seeds_ref_file,
            toolchain,
            patch_sha256,
            pr_head_sha,
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
        for i, (file, func) in enumerate(seeds):
            if not _is_safe_subpath(file):
                LOGGER.warning("Rejecting unsafe seed path: %s", file)
                continue
            src = Path(toolchain.llvm_extract).parent.parent.joinpath(
                "llvm-project-pr", file
            )
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
        return True

    async def _fuzz_loop(
        self,
        work: Path,
        seeds_file: Path,
        seeds_ref_file: Path,
        toolchain: ToolchainPaths,
        patch_sha256: str,
        pr_head_sha: str,
    ) -> FuzzResult:
        config = self._config
        sem = asyncio.Semaphore(config.max_fuzz_parallelism)
        deadline = time.monotonic() + self._fuzz_budget
        idx = 0
        found: asyncio.Event = asyncio.Event()
        result_holder: dict[str, Reproducer | None] = {}

        async def one_iteration(idx: int) -> Reproducer | None:
            async with sem:
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
                    )
                except Exception:
                    LOGGER.exception("Fuzz iteration %s failed", idx)
                    return None

        tasks: list[asyncio.Task[Reproducer | None]] = []
        try:
            while time.monotonic() < deadline and not found.is_set():
                task = asyncio.create_task(one_iteration(idx))
                tasks.append(task)
                idx += 1

                done, pending = await asyncio.wait(
                    tasks, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    result = t.result()
                    if result is not None and not found.is_set():
                        found.set()
                        result_holder["reproducer"] = result
                    tasks.remove(t)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        reproducer = result_holder.get("reproducer")
        if reproducer is not None:
            return FuzzResult(reproducer=reproducer)

        return FuzzResult(reproducer=None)

    async def _fuzz_one(
        self,
        work: Path,
        idx: int,
        seeds_file: Path,
        toolchain: ToolchainPaths,
        patch_sha256: str,
        pr_head_sha: str,
    ) -> Reproducer | None:
        src_file = work / f"correctness-{idx}.src.ll"
        tgt_file = work / f"correctness-{idx}.tgt.ll"

        try:
            await run_command(
                [toolchain.mutate, seeds_file, src_file, FUZZ_RECIPE],
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
                    f"-passes={PASS_NAME}",
                ],
                timeout=60,
                env=min_env,
                memory_limit_bytes=self._opt_memory_limit,
            )
        except CommandError as exc:
            result = exc.result
            if result.returncode < 0:
                source_path = src_file
                reduced = await self._reduce_crash(src_file, toolchain, idx, work)
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
                        f"-passes={PASS_NAME}",
                    ],
                    baseline_revision=toolchain.baseline_revision,
                    pr_head_sha=pr_head_sha,
                    patch_sha256=patch_sha256,
                    stacktrace=result.stderr
                    or result.stdout
                    or f"signal {abs(result.returncode)}",
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
            )
        except asyncio.TimeoutError:
            return None

        stdout = result.stdout
        if "0 incorrect transformations" not in stdout and (
            "Transformation seems to be correct" in stdout
            or "ERROR" in stdout
            or "incorrect" in stdout.lower()
        ):
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
                    f"-passes={PASS_NAME}",
                ],
                baseline_revision=toolchain.baseline_revision,
                pr_head_sha=pr_head_sha,
                patch_sha256=patch_sha256,
                alive2_counterexample=stdout,
            )

        return None

    async def _reduce_crash(
        self,
        src_file: Path,
        toolchain: ToolchainPaths,
        idx: int,
        work: Path,
    ) -> Path | None:
        test_script = work / f"interestingness-{idx}.sh"
        test_script.write_text(
            "#!/bin/bash\n"
            f"'{toolchain.pr_opt}' -S -o /dev/null"
            f" -passes='{PASS_NAME}' '$1' >/dev/null 2>&1\n"
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
