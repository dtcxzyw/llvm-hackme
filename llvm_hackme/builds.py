from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.commands import (
    append_command_log_message,
    minimal_execution_env,
    run_command,
)
from llvm_hackme.config import Config

LOGGER = logging.getLogger(__name__)

LLVM_REPOSITORY = "https://github.com/llvm/llvm-project.git"
ALIVE2_REPOSITORY = "https://github.com/AliveToolkit/alive2.git"


@dataclass(frozen=True)
class ToolchainPaths:
    baseline_revision: str
    baseline_opt: Path
    pr_opt: Path
    llvm_extract: Path
    llvm_reduce: Path
    alive_tv: Path
    mutate: Path
    merge: Path


class BuildManager:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _build_env(self, base_dir: Path) -> dict[str, str]:
        return {
            **minimal_execution_env(),
            "CCACHE_BASEDIR": str(base_dir),
            "CCACHE_DIR": str(self.config.work_dir / "ccache"),
            "CCACHE_NOHASHDIR": "true",
        }

    _BUILD_TIMEOUT = 3600
    _CMDLINE_TIMEOUT = 120

    async def sync_baseline_sources(self) -> tuple[str, str, str]:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        await self._ensure_clone(self.config.llvm_project_dir, LLVM_REPOSITORY)
        await self._ensure_clone(self.config.alive2_dir, ALIVE2_REPOSITORY)

        old_llvm = await self._rev_parse("HEAD", self.config.llvm_project_dir)
        old_alive2 = await self._rev_parse("HEAD", self.config.alive2_dir)

        await run_command(
            ["git", "fetch", "--prune", "origin"],
            cwd=self.config.llvm_project_dir,
            env=minimal_execution_env(),
        )
        await run_command(
            ["git", "fetch", "--prune", "origin"],
            cwd=self.config.alive2_dir,
            env=minimal_execution_env(),
        )

        await self._checkout_rev("origin/main", self.config.llvm_project_dir)
        await self._checkout_rev("origin/master", self.config.alive2_dir)

        new_revision = await self.current_baseline_revision()
        return old_llvm, old_alive2, new_revision

    async def build_baseline_toolchain(self) -> None:
        LOGGER.info("Starting baseline toolchain build")
        await self._configure_and_build_baseline()
        await self._configure_and_build_alive2()
        await self._configure_and_build_fuzz_tools()
        LOGGER.info("Baseline toolchain build complete")

    async def rollback_sources(self, llvm_rev: str, alive2_rev: str) -> None:
        await self._checkout_rev(llvm_rev, self.config.llvm_project_dir)
        await self._checkout_rev(alive2_rev, self.config.alive2_dir)

    async def _rev_parse(self, ref: str, work_dir: Path) -> str:
        result = await run_command(
            ["git", "rev-parse", ref],
            cwd=work_dir,
            env=minimal_execution_env(),
        )
        return result.stdout.strip()

    async def _checkout_rev(self, rev: str, work_dir: Path) -> None:
        await run_command(
            ["git", "checkout", rev],
            cwd=work_dir,
            env=minimal_execution_env(),
        )
        await run_command(
            ["git", "reset", "--hard", rev],
            cwd=work_dir,
            env=minimal_execution_env(),
        )

    async def prepare_pr_worktree(self, patch: str, head_sha: str) -> tuple[str, bool]:
        LOGGER.info("Preparing PR worktree (SHA: %s)", head_sha[:12])
        append_command_log_message(
            f"--- Preparing PR worktree (SHA: {head_sha[:12]}) ---"
        )
        baseline_revision = await self.current_baseline_revision()
        await self._sync_pr_worktree(baseline_revision)
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise RuntimeError(f"Invalid head SHA: {head_sha!r}")
        patch_dir = self.config.work_dir / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patch_dir / f"pr-{head_sha}.patch"
        patch_path.write_text(patch)
        full_patch_applied = await self._apply_patch(patch_path, baseline_revision)
        LOGGER.info("PR worktree ready (full patch applied: %s)", full_patch_applied)
        append_command_log_message(
            f"--- PR worktree ready (full patch: {full_patch_applied}) ---"
        )
        return baseline_revision, full_patch_applied

    async def build_pr_opt(self) -> None:
        LOGGER.info("Building PR opt")
        await self._configure_and_build_pr_opt()
        LOGGER.info("PR opt build done")

    async def _apply_patch(self, patch_path: Path, baseline_revision: str) -> bool:
        cwd = self.config.llvm_project_pr_dir
        apply_args = [
            "git",
            "-c",
            "core.symlinks=false",
            "apply",
            "--3way",
        ]

        try:
            await run_command(
                [*apply_args, str(patch_path)],
                cwd=cwd,
                env=minimal_execution_env(),
            )
            return True
        except Exception:
            LOGGER.warning("Full patch apply failed, retrying source-only")

        await self._reset_worktree(baseline_revision)

        source_only = [
            *apply_args,
            "--exclude=llvm/test/*",
            "--exclude=clang/test/*",
            str(patch_path),
        ]
        try:
            await run_command(
                source_only,
                cwd=cwd,
                env=minimal_execution_env(),
            )
            return False
        except Exception:
            LOGGER.exception("Source-only patch apply also failed, resetting worktree")
            await self._reset_worktree(baseline_revision)
            raise

    async def _reset_worktree(self, baseline_revision: str) -> None:
        cwd = self.config.llvm_project_pr_dir
        with contextlib.suppress(Exception):
            await run_command(
                ["git", "reset", "--hard", baseline_revision],
                cwd=cwd,
                env=minimal_execution_env(),
            )
        with contextlib.suppress(Exception):
            await run_command(
                ["git", "clean", "-ffd"],
                cwd=cwd,
                env=minimal_execution_env(),
            )

    async def current_baseline_revision(self) -> str:
        result = await run_command(
            ["git", "rev-parse", "HEAD"],
            cwd=self.config.llvm_project_dir,
            env=minimal_execution_env(),
        )
        return result.stdout.strip()

    async def current_alive2_revision(self) -> str:
        result = await run_command(
            ["git", "rev-parse", "HEAD"],
            cwd=self.config.alive2_dir,
            env=minimal_execution_env(),
        )
        return result.stdout.strip()

    def toolchain_paths(self, baseline_revision: str) -> ToolchainPaths:
        return ToolchainPaths(
            baseline_revision=baseline_revision,
            baseline_opt=self.config.llvm_build_dir / "bin" / "opt",
            pr_opt=self.config.llvm_build_pr_dir / "bin" / "opt",
            llvm_extract=self.config.llvm_build_dir / "bin" / "llvm-extract",
            llvm_reduce=self.config.llvm_build_dir / "bin" / "llvm-reduce",
            alive_tv=self.config.alive2_build_dir / "alive-tv",
            mutate=self.config.fuzz_tools_build_dir / "mutate",
            merge=self.config.fuzz_tools_build_dir / "merge",
        )

    async def _ensure_clone(self, path: Path, repository: str) -> None:
        if (path / ".git").exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        await run_command(
            ["git", "clone", repository, path],
            env=minimal_execution_env(),
            timeout=1800,
        )

    async def _sync_pr_worktree(self, baseline_revision: str) -> None:
        if not self.config.llvm_project_pr_dir.exists():
            await run_command(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    self.config.llvm_project_pr_dir,
                    baseline_revision,
                ],
                cwd=self.config.llvm_project_dir,
                env=minimal_execution_env(),
            )
        await run_command(
            ["git", "fetch", "--prune", "origin"],
            cwd=self.config.llvm_project_pr_dir,
            env=minimal_execution_env(),
        )
        await run_command(
            ["git", "reset", "--hard", baseline_revision],
            cwd=self.config.llvm_project_pr_dir,
            env=minimal_execution_env(),
        )
        await run_command(
            ["git", "clean", "-ffd"],
            cwd=self.config.llvm_project_pr_dir,
            env=minimal_execution_env(),
        )

    async def _configure_and_build_baseline(self) -> None:
        LOGGER.info("Configuring baseline cmake...")
        append_command_log_message("--- Configuring baseline cmake ---")
        self.config.llvm_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.llvm_project_dir / "llvm"),
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DBUILD_SHARED_LIBS=ON",
                "-G",
                "Ninja",
                "-DLLVM_ENABLE_ASSERTIONS=ON",
                "-DLLVM_INCLUDE_EXAMPLES=OFF",
                "-DLLVM_ENABLE_WARNINGS=OFF",
                "-DLLVM_APPEND_VC_REV=OFF",
                "-DLLVM_TARGETS_TO_BUILD=AArch64;AMDGPU;ARM;BPF;LoongArch;Mips;NVPTX;PowerPC;RISCV;WebAssembly;X86",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                "-DLLVM_ENABLE_RTTI=ON",
                "-DLLVM_ENABLE_EH=ON",
                "-DLLVM_ENABLE_ZSTD=OFF",
            ],
            cwd=self.config.llvm_build_dir,
            env=self._build_env(self.config.llvm_project_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Building baseline opt, llvm-extract, llvm-reduce...")
        append_command_log_message(
            "--- Building baseline opt, llvm-extract, llvm-reduce ---"
        )
        await run_command(
            [
                "cmake",
                "--build",
                ".",
                "-j",
                str(self.config.build_jobs),
                "-t",
                "opt",
                "llvm-extract",
                "llvm-reduce",
            ],
            cwd=self.config.llvm_build_dir,
            env=self._build_env(self.config.llvm_project_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Baseline build complete")
        append_command_log_message("--- Baseline build complete ---")

    async def _configure_and_build_pr_opt(self) -> None:
        LOGGER.info("Configuring PR cmake...")
        append_command_log_message("--- Configuring PR cmake ---")
        self.config.llvm_build_pr_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.llvm_project_pr_dir / "llvm"),
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DBUILD_SHARED_LIBS=ON",
                "-G",
                "Ninja",
                "-DLLVM_ENABLE_ASSERTIONS=ON",
                "-DLLVM_INCLUDE_EXAMPLES=OFF",
                "-DLLVM_ENABLE_WARNINGS=OFF",
                "-DLLVM_APPEND_VC_REV=OFF",
                "-DLLVM_TARGETS_TO_BUILD=AArch64;AMDGPU;ARM;BPF;LoongArch;Mips;NVPTX;PowerPC;RISCV;WebAssembly;X86",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                "-DLLVM_ENABLE_RTTI=ON",
                "-DLLVM_ENABLE_EH=ON",
                "-DLLVM_ENABLE_ZSTD=OFF",
            ],
            cwd=self.config.llvm_build_pr_dir,
            env=self._build_env(self.config.llvm_project_pr_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Building PR opt...")
        append_command_log_message("--- Building PR opt ---")
        await run_command(
            ["cmake", "--build", ".", "-j", str(self.config.build_jobs), "-t", "opt"],
            cwd=self.config.llvm_build_pr_dir,
            env=self._build_env(self.config.llvm_project_pr_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("PR opt build complete")
        append_command_log_message("--- PR opt build complete ---")

    async def _configure_and_build_alive2(self) -> None:
        LOGGER.info("Configuring alive2 cmake...")
        append_command_log_message("--- Configuring alive2 cmake ---")
        self.config.alive2_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.alive2_dir),
                "-GNinja",
                f"-DCMAKE_PREFIX_PATH={self.config.llvm_build_dir}",
                "-DBUILD_TV=1",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ],
            cwd=self.config.alive2_build_dir,
            env=self._build_env(self.config.alive2_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Building alive-tv...")
        append_command_log_message("--- Building alive-tv ---")
        await run_command(
            [
                "cmake",
                "--build",
                ".",
                "-j",
                str(self.config.build_jobs),
                "-t",
                "alive-tv",
            ],
            cwd=self.config.alive2_build_dir,
            env=self._build_env(self.config.alive2_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("alive-tv build complete")
        append_command_log_message("--- alive-tv build complete ---")

    async def _configure_and_build_fuzz_tools(self) -> None:
        LOGGER.info("Configuring fuzz tools cmake...")
        append_command_log_message("--- Configuring fuzz tools cmake ---")
        self.config.fuzz_tools_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(Path(__file__).resolve().parent.parent / "fuzz_tools"),  # noqa: ASYNC240
                "-GNinja",
                f"-DLLVM_DIR={self.config.llvm_build_dir / 'lib' / 'cmake' / 'llvm'}",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ],
            cwd=self.config.fuzz_tools_build_dir,
            env=self._build_env(self.config.llvm_project_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Building fuzz tools...")
        append_command_log_message("--- Building fuzz tools ---")
        await run_command(
            ["cmake", "--build", ".", "-j", str(self.config.build_jobs)],
            cwd=self.config.fuzz_tools_build_dir,
            env=self._build_env(self.config.llvm_project_dir),
            timeout=self._BUILD_TIMEOUT,
        )
        LOGGER.info("Fuzz tools build complete")
        append_command_log_message("--- Fuzz tools build complete ---")
