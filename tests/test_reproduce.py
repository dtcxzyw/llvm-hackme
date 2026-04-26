from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llvm_hackme.commands import CommandError, CommandResult
from llvm_hackme.models import BugKind
from llvm_hackme.reproduce import (
    reproduce,
    reproduce_crash,
    reproduce_miscompilation,
)


class TestReproduceCrash:
    @pytest.mark.asyncio
    async def test_no_crash(self) -> None:
        good_result = CommandResult(args=(), returncode=0, stdout="", stderr="")
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = good_result
            result = await reproduce_crash("/opt/bin/opt", Path("test.ll"))
        assert result.crashed is False
        assert result.stacktrace is None

    @pytest.mark.asyncio
    async def test_crash_with_stderr(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-11, stdout="", stderr="segfault\n")
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await reproduce_crash("/opt/bin/opt", Path("test.ll"))
        assert result.crashed is True
        assert "segfault" in result.stacktrace

    @pytest.mark.asyncio
    async def test_crash_with_stdout_fallback(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-6, stdout="abort\n", stderr="")
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await reproduce_crash("/opt/bin/opt", Path("test.ll"))
        assert result.crashed is True
        assert "abort" in result.stacktrace

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = asyncio.TimeoutError
            result = await reproduce_crash("/opt/bin/opt", Path("test.ll"))
        assert result.crashed is False


class TestReproduceMiscompilation:
    @pytest.mark.asyncio
    async def test_no_miscompilation(self) -> None:
        good_opt = CommandResult(args=(), returncode=0, stdout="", stderr="")
        alive_ok = CommandResult(
            args=(),
            returncode=0,
            stdout="0 incorrect transformations\n",
            stderr="",
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = [good_opt, alive_ok]
            result = await reproduce_miscompilation(
                "/opt/bin/opt", "/opt/alive/tv", Path("test.ll")
            )
        assert result.miscompiled is False
        assert result.alive2_output is None

    @pytest.mark.asyncio
    async def test_miscompilation_detected(self) -> None:
        good_opt = CommandResult(args=(), returncode=0, stdout="", stderr="")
        alive_bad = CommandResult(
            args=(),
            returncode=0,
            stdout="ERROR: Value mismatch\nTransformation seems to be correct\n",
            stderr="",
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = [good_opt, alive_bad]
            result = await reproduce_miscompilation(
                "/opt/bin/opt", "/opt/alive/tv", Path("test.ll")
            )
        assert result.miscompiled is True
        assert "Value mismatch" in result.alive2_output

    @pytest.mark.asyncio
    async def test_opt_crashes_on_miscompilation_reproduce(self) -> None:
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = CommandError(
                CommandResult(args=(), returncode=-11, stdout="", stderr="crash")
            )
            result = await reproduce_miscompilation(
                "/opt/bin/opt", "/opt/alive/tv", Path("test.ll")
            )
        assert result.miscompiled is False

    @pytest.mark.asyncio
    async def test_opt_timeout(self) -> None:
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = asyncio.TimeoutError
            result = await reproduce_miscompilation(
                "/opt/bin/opt", "/opt/alive/tv", Path("test.ll")
            )
        assert result.miscompiled is False


class TestReproduce:
    @pytest.mark.asyncio
    async def test_reproduce_crash(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-11, stdout="", stderr="SIGSEGV")
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await reproduce(BugKind.CRASH, "/opt/bin/opt", Path("test.ll"))
        assert result.crashed is True

    @pytest.mark.asyncio
    async def test_reproduce_miscompilation(self) -> None:
        good_opt = CommandResult(args=(), returncode=0, stdout="", stderr="")
        alive_bad = CommandResult(
            args=(),
            returncode=0,
            stdout="incorrect\n",
            stderr="",
        )
        with patch(
            "llvm_hackme.reproduce.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = [good_opt, alive_bad]
            result = await reproduce(
                BugKind.MISCOMPILATION,
                "/opt/bin/opt",
                Path("test.ll"),
                alive_tv="/opt/alive/tv",
            )
        assert result.miscompiled is True

    def test_reproduce_miscompilation_missing_alive(self) -> None:
        with pytest.raises(ValueError, match="alive_tv"):
            asyncio.run(
                reproduce(
                    BugKind.MISCOMPILATION,
                    "/opt/bin/opt",
                    Path("test.ll"),
                )
            )

    def test_reproduce_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="Unknown bug kind"):
            asyncio.run(
                reproduce(
                    "nonexistent",  # type: ignore[arg-type]
                    "/opt/bin/opt",
                    Path("test.ll"),
                )
            )
