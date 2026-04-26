from __future__ import annotations

from llvm_hackme.paths import is_relevant_pr_file


class TestRelevantPathFilter:
    def test_exact_instcombine_path(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/InstCombine/") is True

    def test_instcombine_file(self) -> None:
        assert (
            is_relevant_pr_file("llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp")
            is True
        )

    def test_instcombine_subdirectory(self) -> None:
        assert (
            is_relevant_pr_file(
                "llvm/lib/Transforms/InstCombine/InstructionCombining.cpp"
            )
            is True
        )

    def test_gvn_is_relevant(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/Scalar/GVN.cpp") is True

    def test_simplify_cfg_is_relevant(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/Utils/SimplifyCFG.cpp") is True

    def test_test_file_is_relevant(self) -> None:
        assert is_relevant_pr_file("llvm/test/Transforms/InstCombine/add.ll") is True

    def test_non_llvm_path(self) -> None:
        assert is_relevant_pr_file("clang/lib/CodeGen/CGExpr.cpp") is False

    def test_unrecognized_pass(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/Scalar/LoopUnroll.cpp") is False

    def test_empty_string(self) -> None:
        assert is_relevant_pr_file("") is False
