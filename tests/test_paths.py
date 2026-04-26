from __future__ import annotations

from llvm_hackme.paths import is_relevant_pr_file


class TestInstCombinePathFilter:
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

    def test_non_instcombine_path(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/Scalar/GVN.cpp") is False

    def test_non_llvm_path(self) -> None:
        assert is_relevant_pr_file("clang/lib/CodeGen/CGExpr.cpp") is False

    def test_instcombine_in_test(self) -> None:
        assert (
            is_relevant_pr_file(
                "llvm/lib/Transforms/InstCombine/with/extra/path/file.cpp"
            )
            is True
        )

    def test_similar_but_different(self) -> None:
        assert is_relevant_pr_file("llvm/lib/Transforms/InstCombine2/foo.cpp") is False

    def test_empty_string(self) -> None:
        assert is_relevant_pr_file("") is False
