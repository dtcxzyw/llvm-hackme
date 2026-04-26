from __future__ import annotations

from unittest.mock import patch

from llvm_hackme.commands import (
    CommandError,
    CommandResult,
    minimal_execution_env,
)


class TestMinimalExecutionEnv:
    def test_basic_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "HOME": "/home/test",
                "PATH": "/bin",
                "TMPDIR": "/tmp",
                "LANG": "en_US",
                "LC_ALL": "en_US",
            },
            clear=True,
        ):
            env = minimal_execution_env()
            assert env["HOME"] == "/home/test"
            assert env["PATH"] == "/bin"
            assert env["TMPDIR"] == "/tmp"
            assert env["LANG"] == "en_US"
            assert env["LC_ALL"] == "en_US"

    def test_minimal_env_excludes_extra(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "LANG": "C",
                "LC_ALL": "C",
                "GITHUB_TOKEN": "secret",
                "OPENAI_AUTH_KEY": "sk-key",
            },
            clear=True,
        ):
            env = minimal_execution_env()
            assert "GITHUB_TOKEN" not in env
            assert "OPENAI_AUTH_KEY" not in env

    def test_minimal_env_with_extra(self) -> None:
        with patch.dict(
            "os.environ",
            {"HOME": "/h", "PATH": "/p", "TMPDIR": "/t", "LANG": "C", "LC_ALL": "C"},
            clear=True,
        ):
            env = minimal_execution_env(extra={"CCACHE_DIR": "/ccache"})
            assert env["CCACHE_DIR"] == "/ccache"


class TestCommandResult:
    def test_command_error(self) -> None:
        result = CommandResult(args=("cmd",), returncode=1, stdout="out", stderr="err")
        error = CommandError(result)
        assert "exit code 1" in str(error)
        assert error.result.returncode == 1
        assert error.result.stderr == "err"
