from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from llvm_hackme.fuzzer import FUNC_RE, FuzzRunner, _reduce_crash, _WorkerContext


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
    def test_reduce_runs_llvm_reduce_and_returns_output(self, tmp_path: Path) -> None:
        src = tmp_path / "test.src.ll"
        src.write_text("define void @f() { ret void }\n")
        work = tmp_path / "work"
        work.mkdir()
        reduced = work / "correctness-0.reduced.ll"
        reduced.write_text("reduced content")

        ctx = _WorkerContext(
            work_dir=str(work),
            seeds_file=str(tmp_path / "seeds.ll"),
            mutate_bin="/bin/mutate",
            opt_bin="/bin/opt",
            alive2_bin="/bin/alive",
            llvm_extract_bin="/bin/extract",
            llvm_reduce_bin="/bin/llvm-reduce",
            pass_name="instcombine",
            baseline_revision="abc",
            pr_head_sha="def",
            patch_sha256="ghi",
            opt_memory_bytes=0,
        )

        with patch("llvm_hackme.fuzzer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _reduce_crash(ctx, 0, src, work)
            assert result is not None
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "/bin/llvm-reduce"

    def test_reduce_falls_back_when_fails(self, tmp_path: Path) -> None:
        src = tmp_path / "test.src.ll"
        src.write_text("define void @f() { ret void }\n")
        work = tmp_path / "work"
        work.mkdir()

        ctx = _WorkerContext(
            work_dir=str(work),
            seeds_file=str(tmp_path / "seeds.ll"),
            mutate_bin="/bin/mutate",
            opt_bin="/bin/opt",
            alive2_bin="/bin/alive",
            llvm_extract_bin="/bin/extract",
            llvm_reduce_bin="/bin/llvm-reduce",
            pass_name="instcombine",
            baseline_revision="abc",
            pr_head_sha="def",
            patch_sha256="ghi",
            opt_memory_bytes=0,
        )

        with patch(
            "llvm_hackme.fuzzer.subprocess.run",
            side_effect=OSError("fail"),
        ):
            result = _reduce_crash(ctx, 0, src, work)
            assert result is None

    def test_reduce_falls_back_on_timeout(self, tmp_path: Path) -> None:
        import subprocess

        src = tmp_path / "test.src.ll"
        src.write_text("define void @f() { ret void }\n")
        work = tmp_path / "work"
        work.mkdir()

        ctx = _WorkerContext(
            work_dir=str(work),
            seeds_file=str(tmp_path / "seeds.ll"),
            mutate_bin="/bin/mutate",
            opt_bin="/bin/opt",
            alive2_bin="/bin/alive",
            llvm_extract_bin="/bin/extract",
            llvm_reduce_bin="/bin/llvm-reduce",
            pass_name="instcombine",
            baseline_revision="abc",
            pr_head_sha="def",
            patch_sha256="ghi",
            opt_memory_bytes=0,
        )

        with patch(
            "llvm_hackme.fuzzer.subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 30),
        ):
            result = _reduce_crash(ctx, 0, src, work)
            assert result is None
