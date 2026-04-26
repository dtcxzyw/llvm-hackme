from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from llvm_hackme.config import Config


class TestConfigFromEnv:
    def test_required_env_vars_raise(self) -> None:
        with (
            patch.dict(os.environ, clear=True),
            pytest.raises(RuntimeError, match="GITHUB_TOKEN"),
        ):
            Config.from_env()

    def test_minimal_env(self) -> None:
        print(os.environ.items())
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "gh_token",
                "OPENAI_ENDPOINT": "https://api.example.com",
                "OPENAI_AUTH_KEY": "sk-key",
                "OPENAI_MODEL": "test-model",
            },
            clear=True,
        ):
            config = Config.from_env()
        assert config.github_token == "gh_token"
        assert config.openai_endpoint == "https://api.example.com"
        assert config.openai_auth_key == "sk-key"
        assert config.openai_model == "test-model"

    def test_all_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "gh_token",
                "OPENAI_ENDPOINT": "https://api.example.com",
                "OPENAI_AUTH_KEY": "sk-key",
                "OPENAI_MODEL": "test-model",
            },
            clear=True,
        ):
            config = Config.from_env()
        assert config.github_repository == "llvm/llvm-project"
        assert config.github_login_override is None
        assert config.scan_interval_seconds == 60
        assert config.scan_overlap_seconds == 300
        assert config.debounce_seconds == 300
        assert config.baseline_update_interval_seconds == 3600
        assert config.fuzz_budget_seconds == 600
        assert config.max_patch_chars == 200_000
        assert config.patch_chunk_chars == 50_000
        assert config.max_patch_chunks == 8
        assert config.opt_memory_limit_bytes == 1024 * 1024 * 1024
        assert config.max_fuzz_parallelism == 1

    def test_custom_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "gh_token",
                "OPENAI_ENDPOINT": "https://api.example.com",
                "OPENAI_AUTH_KEY": "sk-key",
                "OPENAI_MODEL": "test-model",
                "LLVM_HACKME_GITHUB_REPOSITORY": "custom/repo",
                "LLVM_HACKME_GITHUB_LOGIN": "test-user",
                "LLVM_HACKME_SCAN_INTERVAL_SECONDS": "30",
                "LLVM_HACKME_DEBOUNCE_SECONDS": "120",
                "LLVM_HACKME_FUZZ_BUDGET_SECONDS": "300",
                "LLVM_HACKME_MAX_PATCH_CHARS": "10000",
                "LLVM_HACKME_WORK_DIR": "/tmp/test-work",
            },
            clear=True,
        ):
            config = Config.from_env()
        assert config.github_repository == "custom/repo"
        assert config.github_login_override == "test-user"
        assert config.scan_interval_seconds == 30
        assert config.debounce_seconds == 120
        assert config.fuzz_budget_seconds == 300
        assert config.max_patch_chars == 10000
        assert config.work_dir == Path("/tmp/test-work")

    def test_derived_paths(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "gh_token",
                "OPENAI_ENDPOINT": "https://api.example.com",
                "OPENAI_AUTH_KEY": "sk-key",
                "OPENAI_MODEL": "test-model",
                "LLVM_HACKME_WORK_DIR": "/tmp/test-work",
            },
            clear=True,
        ):
            config = Config.from_env()
        assert config.llvm_project_dir == Path("/tmp/test-work/llvm-project")
        assert config.llvm_build_dir == Path("/tmp/test-work/llvm-build")
        assert config.llvm_project_pr_dir == Path("/tmp/test-work/llvm-project-pr")
        assert config.llvm_build_pr_dir == Path("/tmp/test-work/llvm-build-pr")
        assert config.alive2_dir == Path("/tmp/test-work/alive2")
        assert config.alive2_build_dir == Path("/tmp/test-work/alive2-build")
        assert config.fuzz_work_dir == Path("/tmp/test-work/fuzz")
        assert config.fuzz_tools_build_dir == Path("/tmp/test-work/fuzz-tools-build")
