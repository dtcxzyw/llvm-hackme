from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llvm_hackme.fuzzer import FUNC_RE, FuzzRunner


class TestCollectSeeds:
    def test_extracts_function_from_ll_file_in_patch(self) -> None:
        runner = FuzzRunner(MagicMock())
        patch = (
            "diff --git a/llvm/test/Transforms/InstCombine/test.ll b/test.ll\n"
            "--- a/test.ll\n"
            "+++ b/test.ll\n"
            "@@ -1,4 +1,4 @@\n"
            " define i32 @foo(i32 %x) {\n"
            "   ret i32 %x\n"
            " }\n"
            "+define i64 @bar(i64 %y) {\n"
            "+  ret i64 %y\n"
            "+}\n"
        )
        seeds = runner._collect_seeds(patch)
        assert len(seeds) == 2
        assert ("llvm/test/Transforms/InstCombine/test.ll", "foo") in seeds
        assert ("llvm/test/Transforms/InstCombine/test.ll", "bar") in seeds

    def test_skips_non_ll_files(self) -> None:
        runner = FuzzRunner(MagicMock())
        patch = (
            "diff --git a/llvm/lib/Transforms/InstCombine/IC.cpp b/IC.cpp\n"
            " define i32 @foo(i32 %x) {\n"
            "   ret i32 %x\n"
            " }\n"
        )
        seeds = runner._collect_seeds(patch)
        assert len(seeds) == 0

    def test_deduplicates_same_func_in_file(self) -> None:
        runner = FuzzRunner(MagicMock())
        patch = (
            "diff --git a/test.ll b/test.ll\n"
            " define i32 @foo(i32 %x) {\n"
            " define i32 @foo(i32 %x) {\n"
        )
        seeds = runner._collect_seeds(patch)
        assert len(seeds) == 1

    def test_empty_patch(self) -> None:
        runner = FuzzRunner(MagicMock())
        seeds = runner._collect_seeds("")
        assert len(seeds) == 0


class TestFuncRE:
    def test_matches_define_with_function_name(self) -> None:
        m = FUNC_RE.search("define i32 @my_func(i32 %x) {")
        assert m is not None
        assert m.group(1) == "my_func"

    def test_matches_hyphenated_name(self) -> None:
        m = FUNC_RE.search("define void @foo-bar(i8) local_unnamed_addr {")
        assert m is not None
        assert m.group(1) == "foo-bar"

    def test_no_match_on_declare(self) -> None:
        m = FUNC_RE.search("declare void @external()")
        assert m is None


class TestReduceCrash:
    @pytest.mark.asyncio
    async def test_reduce_runs_llvm_reduce_and_returns_output(
        self, tmp_path: Path
    ) -> None:
        runner = FuzzRunner(MagicMock())
        src = tmp_path / "test.src.ll"
        src.write_text("define void @f() { ret void }\n")
        work = tmp_path / "work"
        work.mkdir()
        tc = MagicMock()
        tc.pr_opt = Path("/opt/bin/opt")
        tc.llvm_reduce = Path("/opt/bin/llvm-reduce")

        with patch(
            "llvm_hackme.fuzzer.run_command", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            reduced = work / "correctness-0.reduced.ll"
            reduced.write_text("reduced content")

            result = await runner._reduce_crash(src, tc, 0, work, "instcombine")

            assert result is not None
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == tc.llvm_reduce

    @pytest.mark.asyncio
    async def test_reduce_falls_back_when_fails(self, tmp_path: Path) -> None:
        runner = FuzzRunner(MagicMock())
        src = tmp_path / "test.src.ll"
        src.write_text("define void @f() { ret void }\n")
        work = tmp_path / "work"
        work.mkdir()
        tc = MagicMock()
        tc.pr_opt = Path("/opt/bin/opt")
        tc.llvm_reduce = Path("/opt/bin/llvm-reduce")

        from llvm_hackme.commands import CommandError, CommandResult

        error_result = CommandResult(
            args=("/opt/bin/llvm-reduce",), returncode=1, stdout="", stderr="fail"
        )

        with patch(
            "llvm_hackme.fuzzer.run_command",
            new_callable=AsyncMock,
            side_effect=CommandError(error_result),
        ):
            result = await runner._reduce_crash(src, tc, 0, work, "instcombine")

            assert result is None
