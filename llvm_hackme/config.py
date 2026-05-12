from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"Environment variable {name} must be an integer, got: {value!r}"
        ) from None


@dataclass(frozen=True)
class Config:
    github_token: str
    github_repository: str
    openai_endpoint: str
    openai_auth_key: str
    openai_model: str
    github_login_override: str | None
    work_dir: Path
    state_db: Path
    scan_interval_seconds: int
    scan_overlap_seconds: int
    scan_iteration_timeout_seconds: int
    debounce_seconds: int
    baseline_update_interval_seconds: int
    fuzz_budget_seconds: int
    hack_crash_budget_seconds: int
    hack_miscomp_budget_seconds: int
    hack_model: str
    max_patch_chars: int
    max_review_retries: int
    opt_memory_limit_bytes: int
    build_jobs: int

    @property
    def llvm_project_dir(self) -> Path:
        return self.work_dir / "llvm-project"

    @property
    def llvm_build_dir(self) -> Path:
        return self.work_dir / "llvm-build"

    @property
    def llvm_project_pr_dir(self) -> Path:
        return self.work_dir / "llvm-project-pr"

    @property
    def llvm_build_pr_dir(self) -> Path:
        return self.work_dir / "llvm-build-pr"

    @property
    def alive2_dir(self) -> Path:
        return self.work_dir / "alive2"

    @property
    def alive2_build_dir(self) -> Path:
        return self.work_dir / "alive2-build"

    @property
    def fuzz_work_dir(self) -> Path:
        return self.work_dir / "fuzz"

    @property
    def hack_work_dir(self) -> Path:
        return self.work_dir / "hack"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def hack_context_file(self) -> Path:
        return self.hack_work_dir / "context.json"

    @property
    def fuzz_tools_build_dir(self) -> Path:
        return self.work_dir / "fuzz-tools-build"

    @classmethod
    def from_env(cls) -> Config:
        work_dir = Path(
            os.environ.get("LLVM_HACKME_WORK_DIR", "work/llvm-hackme")
        ).resolve()
        return cls(
            github_token=_required_env("GITHUB_TOKEN"),
            github_repository=os.environ.get(
                "LLVM_HACKME_GITHUB_REPOSITORY", "llvm/llvm-project"
            ),
            openai_endpoint=_required_env("OPENAI_ENDPOINT"),
            openai_auth_key=_required_env("OPENAI_AUTH_KEY"),
            openai_model=_required_env("OPENAI_MODEL"),
            github_login_override=os.environ.get("LLVM_HACKME_GITHUB_LOGIN"),
            work_dir=work_dir,
            state_db=Path(
                os.environ.get("LLVM_HACKME_STATE_DB", work_dir / "state.db")
            ),
            scan_interval_seconds=_int_env("LLVM_HACKME_SCAN_INTERVAL_SECONDS", 60),
            scan_overlap_seconds=_int_env("LLVM_HACKME_SCAN_OVERLAP_SECONDS", 300),
            scan_iteration_timeout_seconds=_int_env(
                "LLVM_HACKME_SCAN_ITERATION_TIMEOUT_SECONDS", 300
            ),
            debounce_seconds=_int_env("LLVM_HACKME_DEBOUNCE_SECONDS", 300),
            baseline_update_interval_seconds=_int_env(
                "LLVM_HACKME_BASELINE_UPDATE_INTERVAL_SECONDS", 3600
            ),
            fuzz_budget_seconds=_int_env("LLVM_HACKME_FUZZ_BUDGET_SECONDS", 120),
            hack_crash_budget_seconds=_int_env(
                "LLVM_HACKME_HACK_CRASH_BUDGET_SECONDS", 600
            ),
            hack_miscomp_budget_seconds=_int_env(
                "LLVM_HACKME_HACK_MISCOMP_BUDGET_SECONDS", 600
            ),
            hack_model=_required_env("LLVM_HACKME_HACK_MODEL"),
            max_patch_chars=_int_env("LLVM_HACKME_MAX_PATCH_CHARS", 200_000),
            max_review_retries=_int_env("LLVM_HACKME_MAX_REVIEW_RETRIES", 2),
            opt_memory_limit_bytes=_int_env(
                "LLVM_HACKME_OPT_MEMORY_LIMIT_BYTES", 1024 * 1024 * 1024
            ),
            build_jobs=_int_env("LLVM_HACKME_BUILD_JOBS", 32),
        )
