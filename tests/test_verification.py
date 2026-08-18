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
    _validate_ir_llvm_intrinsics,
    _validate_ir_vscale_target,
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


class TestValidateIrLlvmIntrinsics:
    def test_allows_declare_and_call(self) -> None:
        ir = (
            "declare void @llvm.assume(i1)\n"
            "define i32 @f(i1 %c) {\n"
            "  call void @llvm.assume(i1 %c)\n"
            "  ret i32 0\n"
            "}\n"
        )
        assert _validate_ir_llvm_intrinsics(ir) is None

    def test_rejects_llvm_global_definition(self) -> None:
        ir = "@llvm.foo = global i32 0\n\ndefine i32 @f() { ret i32 0 }\n"
        reason = _validate_ir_llvm_intrinsics(ir)
        assert reason is not None
        assert "@llvm.foo" in reason

    def test_rejects_llvm_constant_definition(self) -> None:
        ir = "@llvm.foo = constant i32 1\n\ndefine i32 @f() { ret i32 0 }\n"
        assert _validate_ir_llvm_intrinsics(ir) is not None

    def test_rejects_llvm_global_operand(self) -> None:
        ir = (
            "@llvm.foo = global i32 0\n"
            "define i32 @f() {\n"
            "  %v = load i32, ptr @llvm.foo\n"
            "  ret i32 %v\n"
            "}\n"
        )
        assert _validate_ir_llvm_intrinsics(ir) is not None

    def test_rejects_define_of_llvm_function(self) -> None:
        ir = "define void @llvm.foo() { ret void }\n"
        reason = _validate_ir_llvm_intrinsics(ir)
        assert reason is not None
        assert "defining" in reason

    def test_rejects_intrinsic_as_call_argument(self) -> None:
        ir = (
            "declare i32 @llvm.foo()\n"
            "define i32 @f() {\n"
            "  %r = call i32 @bar(ptr @llvm.foo)\n"
            "  ret i32 0\n"
            "}\n"
        )
        assert _validate_ir_llvm_intrinsics(ir) is not None

    def test_ignores_full_line_comments(self) -> None:
        ir = "; CHECK: call void @llvm.assume(i1 %c)\n\ndefine i32 @f() { ret i32 0 }\n"
        assert _validate_ir_llvm_intrinsics(ir) is None


class TestValidateIrVscaleTarget:
    VSCALE_AARCH64 = (
        'target datalayout = "e-m:e-i64:64-n32:64-S128"\n'
        'target triple = "aarch64-unknown-linux-gnu"\n'
        "define <vscale x 2 x i32> @f(<vscale x 2 x i32> %x) {\n"
        "  ret <vscale x 2 x i32> %x\n"
        "}\n"
    )

    def test_allows_ir_without_vscale(self) -> None:
        ir = "define i32 @f() { ret i32 0 }\n"
        assert _validate_ir_vscale_target(ir, []) is None

    def test_ignores_vscale_in_comments(self) -> None:
        ir = "; uses vscale\n\ndefine i32 @f() { ret i32 0 }\n"
        assert _validate_ir_vscale_target(ir, []) is None

    def test_rejects_vscale_without_datalayout(self) -> None:
        ir = self.VSCALE_AARCH64.split("\n", 1)[1]
        reason = _validate_ir_vscale_target(ir, ["-mattr=sve2"])
        assert reason is not None
        assert "target datalayout" in reason

    def test_rejects_vscale_without_triple(self) -> None:
        ir = "\n".join(
            line
            for line in self.VSCALE_AARCH64.split("\n")
            if "target triple" not in line
        )
        reason = _validate_ir_vscale_target(ir, ["-mattr=sve2"])
        assert reason is not None
        assert "target triple" in reason

    def test_rejects_unsupported_triple(self) -> None:
        ir = self.VSCALE_AARCH64.replace(
            "aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"
        )
        reason = _validate_ir_vscale_target(ir, [])
        assert reason is not None
        assert "x86_64" in reason
        assert "aarch64" in reason

    def test_rejects_aarch64_without_sve2(self) -> None:
        reason = _validate_ir_vscale_target(self.VSCALE_AARCH64, [])
        assert reason is not None
        assert "-mattr=sve2" in reason

    def test_allows_aarch64_with_sve2(self) -> None:
        assert _validate_ir_vscale_target(self.VSCALE_AARCH64, ["-mattr=sve2"]) is None

    def test_rejects_riscv64_without_plus_v(self) -> None:
        ir = self.VSCALE_AARCH64.replace(
            "aarch64-unknown-linux-gnu", "riscv64-unknown-linux-gnu"
        )
        reason = _validate_ir_vscale_target(ir, [])
        assert reason is not None
        assert "-mattr=+v" in reason

    def test_allows_riscv64_with_plus_v(self) -> None:
        ir = self.VSCALE_AARCH64.replace(
            "aarch64-unknown-linux-gnu", "riscv64-unknown-linux-gnu"
        )
        assert _validate_ir_vscale_target(ir, ["-mattr=+v"]) is None


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
        assert reason.startswith("Baseline opt also crashes — not a PR regression.")
        mock_check.assert_called_once()  # only baseline checked

    @pytest.mark.asyncio
    async def test_verify_crash_rejects_llvm_global(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=(
                "@llvm.foo = global i32 0\n\ndefine i32 @f() { ret i32 0 }\n"
            ),
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
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is None
        assert "@llvm.foo" in reason
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_crash_rejects_vscale_without_mattr(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=(
                'target datalayout = "e-m:e-i64:64-n32:64-S128"\n'
                'target triple = "aarch64-unknown-linux-gnu"\n'
                "define <vscale x 2 x i32> @f() {"
                " ret <vscale x 2 x i32> zeroinitializer }\n"
            ),
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
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is None
        assert "-mattr=sve2" in reason
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_crash_allows_llvm_declare_and_call(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=(
                "declare void @llvm.assume(i1)\n"
                "define i32 @f(i1 %c) {\n"
                "  call void @llvm.assume(i1 %c)\n"
                "  ret i32 0\n"
                "}\n"
            ),
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
        assert result.source_content == (
            "define i32 @f(i1 %c) {\n  call void @llvm.assume(i1 %c)\n  ret i32 0\n}\n"
        )

    @pytest.mark.asyncio
    async def test_verify_miscompilation_rejects_llvm_operand(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=Path("test.ll"),
            command=["opt", "-S", "test.ll"],
            baseline_revision="rev",
            pr_head_sha="sha",
            patch_sha256="p2",
            source_content=(
                "@llvm.foo = global i32 0\n"
                "define i32 @f() {\n"
                "  %v = load i32, ptr @llvm.foo\n"
                "  ret i32 %v\n"
                "}\n"
            ),
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
            result, reason = await verify_reproducer(
                reproducer, toolchain, ["-passes=instcombine"]
            )
        assert result is None
        assert "@llvm.foo" in reason
        mock_check.assert_not_called()

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
