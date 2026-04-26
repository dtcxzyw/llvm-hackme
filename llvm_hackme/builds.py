from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from llvm_hackme.commands import run_command
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

    async def update_baseline(self) -> str:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        await self._ensure_clone(self.config.llvm_project_dir, LLVM_REPOSITORY)
        await self._ensure_clone(self.config.alive2_dir, ALIVE2_REPOSITORY)
        await run_command(
            ["git", "fetch", "--prune", "origin"], cwd=self.config.llvm_project_dir
        )
        await run_command(
            ["git", "checkout", "origin/main"], cwd=self.config.llvm_project_dir
        )
        await run_command(
            ["git", "reset", "--hard", "origin/main"], cwd=self.config.llvm_project_dir
        )
        await run_command(
            ["git", "fetch", "--prune", "origin"], cwd=self.config.alive2_dir
        )
        await run_command(
            ["git", "checkout", "origin/master"], cwd=self.config.alive2_dir
        )
        await run_command(
            ["git", "reset", "--hard", "origin/master"], cwd=self.config.alive2_dir
        )
        revision = await self.current_baseline_revision()
        await self._configure_and_build_baseline()
        await self._configure_and_build_alive2()
        await self._configure_and_build_fuzz_tools()
        return revision

    async def prepare_pr_build(self, patch: str, head_sha: str) -> ToolchainPaths:
        baseline_revision = await self.current_baseline_revision()
        await self._sync_pr_worktree(baseline_revision)
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise RuntimeError(f"Invalid head SHA: {head_sha!r}")
        patch_path = self.config.work_dir / f"pr-{head_sha}.patch"
        patch_path.write_text(patch)
        await run_command(
            [
                "git",
                "-c",
                "core.symlinks=false",
                "apply",
                "--ignore-space-change",
                str(patch_path),
            ],
            cwd=self.config.llvm_project_pr_dir,
        )
        await self._configure_and_build_pr_opt()
        return self.toolchain_paths(baseline_revision)

    async def current_baseline_revision(self) -> str:
        result = await run_command(
            ["git", "rev-parse", "HEAD"], cwd=self.config.llvm_project_dir
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
        await run_command(["git", "clone", repository, path])

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
            )
        await run_command(
            ["git", "fetch", "--prune", "origin"], cwd=self.config.llvm_project_pr_dir
        )
        await run_command(
            ["git", "reset", "--hard", baseline_revision],
            cwd=self.config.llvm_project_pr_dir,
        )
        await run_command(["git", "clean", "-ffd"], cwd=self.config.llvm_project_pr_dir)

    async def _configure_and_build_baseline(self) -> None:
        self.config.llvm_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.llvm_project_dir / "llvm"),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=ON",
                "-G",
                "Ninja",
                "-DLLVM_ENABLE_ASSERTIONS=ON",
                "-DLLVM_INCLUDE_EXAMPLES=OFF",
                "-DLLVM_ENABLE_WARNINGS=OFF",
                "-DLLVM_APPEND_VC_REV=OFF",
                "-DLLVM_TARGETS_TO_BUILD=X86;",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                "-DLLVM_ENABLE_RTTI=ON",
                "-DLLVM_ENABLE_EH=ON",
                "-DLLVM_ENABLE_ZSTD=OFF",
            ],
            cwd=self.config.llvm_build_dir,
            env=self._ccache_env(self.config.llvm_project_dir),
        )
        await run_command(
            [
                "cmake",
                "--build",
                ".",
                "-j",
                "32",
                "-t",
                "opt",
                "llvm-extract",
                "llvm-reduce",
            ],
            cwd=self.config.llvm_build_dir,
            env=self._ccache_env(self.config.llvm_project_dir),
        )

    async def _configure_and_build_pr_opt(self) -> None:
        self.config.llvm_build_pr_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.llvm_project_pr_dir / "llvm"),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=ON",
                "-G",
                "Ninja",
                "-DLLVM_ENABLE_ASSERTIONS=ON",
                "-DLLVM_INCLUDE_EXAMPLES=OFF",
                "-DLLVM_ENABLE_WARNINGS=OFF",
                "-DLLVM_APPEND_VC_REV=OFF",
                "-DLLVM_TARGETS_TO_BUILD=X86;",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                "-DLLVM_ENABLE_RTTI=ON",
                "-DLLVM_ENABLE_EH=ON",
                "-DLLVM_ENABLE_ZSTD=OFF",
            ],
            cwd=self.config.llvm_build_pr_dir,
            env=self._ccache_env(self.config.llvm_project_pr_dir),
        )
        await run_command(
            ["cmake", "--build", ".", "-j", "32", "-t", "opt"],
            cwd=self.config.llvm_build_pr_dir,
            env=self._ccache_env(self.config.llvm_project_pr_dir),
        )

    async def _configure_and_build_alive2(self) -> None:
        self.config.alive2_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(self.config.alive2_dir),
                "-GNinja",
                f"-DCMAKE_PREFIX_PATH={self.config.llvm_build_dir}",
                "-DBUILD_TV=1",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ],
            cwd=self.config.alive2_build_dir,
            env=self._ccache_env(self.config.alive2_dir),
        )
        await run_command(
            ["cmake", "--build", ".", "-j", "32", "-t", "alive-tv"],
            cwd=self.config.alive2_build_dir,
            env=self._ccache_env(self.config.alive2_dir),
        )

    async def _configure_and_build_fuzz_tools(self) -> None:
        self.config.fuzz_tools_build_dir.mkdir(parents=True, exist_ok=True)
        await run_command(
            [
                "cmake",
                str(Path(__file__).resolve().parent.parent / "fuzz_tools"),  # noqa: ASYNC240
                "-GNinja",
                f"-DLLVM_DIR={self.config.llvm_build_dir / 'lib' / 'cmake' / 'llvm'}",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ],
            cwd=self.config.fuzz_tools_build_dir,
            env=self._ccache_env(self.config.llvm_project_dir),
        )
        await run_command(
            ["cmake", "--build", ".", "-j"],
            cwd=self.config.fuzz_tools_build_dir,
            env=self._ccache_env(self.config.llvm_project_dir),
        )

    def _ccache_env(self, base_dir: Path) -> dict[str, str]:
        return {
            "CCACHE_BASEDIR": str(base_dir),
            "CCACHE_DIR": str(self.config.work_dir / "ccache"),
            "CCACHE_NOHASHDIR": "true",
        }
