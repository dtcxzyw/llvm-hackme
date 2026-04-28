from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llvm_hackme.builds import ToolchainPaths
from llvm_hackme.commands import CommandError, CommandResult
from llvm_hackme.models import BugKind, Reproducer
from llvm_hackme.verification import (
    CrashInfo,
    MiscompilationInfo,
    check_crash,
    check_miscompilation,
    verify_reproducer,
)

IR_CONTENT = "define void @f() { ret void }"


class TestCheckCrash:
    @pytest.mark.asyncio
    async def test_no_crash(self) -> None:
        good = CommandResult(args=(), returncode=0, stdout="", stderr="")
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = good
            result = await check_crash(
                "/opt/bin/opt", IR_CONTENT, ["-passes=instcombine"]
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_crash_with_stderr(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-11, stdout="", stderr="SIGSEGV\n")
        )
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await check_crash(
                "/opt/bin/opt", IR_CONTENT, ["-passes=instcombine"]
            )
        assert result is not None
        assert result.stacktrace == "SIGSEGV\n"

    @pytest.mark.asyncio
    async def test_crash_stdout_fallback(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-6, stdout="abort\n", stderr="")
        )
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await check_crash(
                "/opt/bin/opt", IR_CONTENT, ["-passes=instcombine"]
            )
        assert result is not None
        assert "abort" in result.stacktrace

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = asyncio.TimeoutError
            result = await check_crash(
                "/opt/bin/opt", IR_CONTENT, ["-passes=instcombine"]
            )
        assert result is None


class TestCheckMiscompilation:
    @pytest.mark.asyncio
    async def test_no_miscompilation(self) -> None:
        good_opt = CommandResult(args=(), returncode=0, stdout="", stderr="")
        alive_ok = CommandResult(
            args=(),
            returncode=0,
            stdout="0 incorrect transformations\nTransformation seems to be correct\n",
            stderr="",
        )
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = [good_opt, alive_ok]
            result = await check_miscompilation(
                "/opt/bin/opt",
                "/opt/alive/tv",
                IR_CONTENT,
                ["-passes=instcombine"],
            )
        assert result is None

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
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = [good_opt, alive_bad]
            result = await check_miscompilation(
                "/opt/bin/opt",
                "/opt/alive/tv",
                IR_CONTENT,
                ["-passes=instcombine"],
            )
        assert result is not None
        assert "Value mismatch" in result.alive2_output

    @pytest.mark.asyncio
    async def test_opt_crashes(self) -> None:
        exc = CommandError(
            CommandResult(args=(), returncode=-11, stdout="", stderr="crash")
        )
        with patch(
            "llvm_hackme.verification.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = exc
            result = await check_miscompilation(
                "/opt/bin/opt",
                "/opt/alive/tv",
                IR_CONTENT,
                ["-passes=instcombine"],
            )
        assert result is None


class TestVerifyReproducer:
    @pytest.mark.asyncio
    async def test_verify_crash_regression(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=IR_CONTENT,
        )
        toolchain = ToolchainPaths(
            baseline_opt=Path("/opt/baseline/opt"),
            pr_opt=Path("/opt/pr/opt"),
            llvm_extract=Path("/opt/llvm-extract"),
            merge=Path("/opt/merge"),
            mutate=Path("/opt/mutate"),
            alive_tv=Path("/opt/alive/tv"),
            baseline_revision="rev",
            llvm_reduce=Path("/opt/llvm-reduce"),
        )

        with patch(
            "llvm_hackme.verification.check_crash", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = [None, CrashInfo(stacktrace="SIGSEGV")]
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is not None
        assert reason == ""
        assert result.kind == BugKind.CRASH
        assert result.stacktrace == "SIGSEGV"

    @pytest.mark.asyncio
    async def test_verify_crash_baseline_also_crashes(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=IR_CONTENT,
        )
        toolchain = ToolchainPaths(
            baseline_opt=Path("/opt/baseline/opt"),
            pr_opt=Path("/opt/pr/opt"),
            llvm_extract=Path("/opt/llvm-extract"),
            merge=Path("/opt/merge"),
            mutate=Path("/opt/mutate"),
            alive_tv=Path("/opt/alive/tv"),
            baseline_revision="rev",
            llvm_reduce=Path("/opt/llvm-reduce"),
        )

        with patch(
            "llvm_hackme.verification.check_crash", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = [
                CrashInfo(stacktrace="SIGSEGV"),  # baseline crashes too
                CrashInfo(stacktrace="SIGSEGV"),
            ]
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is None
        assert reason == "Baseline opt also crashes — not a PR regression"
        mock_check.assert_called_once()  # only baseline checked

    @pytest.mark.asyncio
    async def test_verify_miscompilation_regression(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=IR_CONTENT,
        )
        toolchain = ToolchainPaths(
            baseline_opt=Path("/opt/baseline/opt"),
            pr_opt=Path("/opt/pr/opt"),
            llvm_extract=Path("/opt/llvm-extract"),
            merge=Path("/opt/merge"),
            mutate=Path("/opt/mutate"),
            alive_tv=Path("/opt/alive/tv"),
            baseline_revision="rev",
            llvm_reduce=Path("/opt/llvm-reduce"),
        )

        with patch(
            "llvm_hackme.verification.check_miscompilation",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.side_effect = [
                None,  # baseline clean
                MiscompilationInfo(alive2_output="Value mismatch"),
            ]
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is not None
        assert reason == ""
        assert result.kind == BugKind.MISCOMPILATION
        assert "Value mismatch" in result.alive2_counterexample
