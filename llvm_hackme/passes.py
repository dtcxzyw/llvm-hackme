from __future__ import annotations

_TEST_KEYWORDS: list[tuple[str, str]] = [
    ("llvm/test/Transforms/InstCombine", "instcombine<no-verify-fixpoint>"),
    ("llvm/test/Transforms/InstSimplify", "instcombine<no-verify-fixpoint>"),
    ("llvm/test/Analysis/ValueTracking", "instcombine<no-verify-fixpoint>"),
    (
        "llvm/test/Transforms/ConstraintElimination",
        "constraint-elimination",
    ),
    ("llvm/test/Transforms/EarlyCSE", "early-cse"),
    ("llvm/test/Transforms/GVN", "gvn"),
    ("llvm/test/Transforms/NewGVN", "newgvn"),
    ("llvm/test/Transforms/Reassociate", "reassociate"),
    ("llvm/test/Transforms/SCCP", "sccp"),
    (
        "llvm/test/Transforms/CorrelatedValuePropagation",
        "correlated-propagation",
    ),
    ("llvm/test/Transforms/SimplifyCFG", "simplifycfg"),
    ("llvm/test/Transforms/VectorCombine", "vector-combine"),
    (
        "llvm/test/Transforms/AggressiveInstCombine",
        "aggressive-instcombine",
    ),
]

_O3_KEYWORD: list[tuple[str, str]] = [
    ("llvm/test/Transforms/PhaseOrdering", "default<O3>"),
]

_SOURCE_KEYWORDS: list[tuple[str, str]] = [
    ("llvm/lib/Transforms/InstCombine", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Analysis/InstructionSimplify", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Analysis/ValueTracking", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Analysis/ConstantFolding", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/IR/ConstantFold", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/IR/ConstantRange", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/IR/ConstantFPRange", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Support/KnownBits", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Support/KnownFPClass", "instcombine<no-verify-fixpoint>"),
    (
        "llvm/include/llvm/Analysis/InstructionSimplify.h",
        "instcombine<no-verify-fixpoint>",
    ),
    (
        "llvm/include/llvm/Analysis/ValueTracking.h",
        "instcombine<no-verify-fixpoint>",
    ),
    (
        "llvm/include/llvm/Analysis/ConstantFolding.h",
        "instcombine<no-verify-fixpoint>",
    ),
    (
        "llvm/include/llvm/Support/KnownBits.h",
        "instcombine<no-verify-fixpoint>",
    ),
    (
        "llvm/include/llvm/Support/KnownFPClass.h",
        "instcombine<no-verify-fixpoint>",
    ),
    (
        "llvm/lib/Transforms/ConstraintElimination",
        "constraint-elimination",
    ),
    ("llvm/lib/Transforms/Scalar/EarlyCSE", "early-cse"),
    ("llvm/lib/Transforms/Scalar/GVN", "gvn"),
    ("llvm/lib/Transforms/Scalar/NewGVN", "newgvn"),
    ("llvm/lib/Transforms/Scalar/Reassociate", "reassociate"),
    ("llvm/lib/Transforms/Scalar/SCCP", "sccp"),
    (
        "llvm/lib/Transforms/Scalar/CorrelatedValuePropagation",
        "correlated-propagation",
    ),
    ("llvm/lib/Transforms/Utils/SimplifyCFG.cpp", "simplifycfg"),
    ("llvm/lib/Transforms/Vectorize/VectorCombine", "vector-combine"),
    (
        "llvm/lib/Transforms/AggressiveInstCombine",
        "aggressive-instcombine",
    ),
]

ALL_KEYWORDS: list[tuple[str, str]] = _TEST_KEYWORDS + _O3_KEYWORD + _SOURCE_KEYWORDS


def guess_pass_name(patch_text: str) -> str | None:
    for line in patch_text.split("\n"):
        if line.startswith("diff --git a/"):
            file_path = line.removeprefix("diff --git a/").split(" ", 1)[0]
            for keyword, pass_name in _TEST_KEYWORDS:
                if file_path.startswith(keyword):
                    return pass_name
    for line in patch_text.split("\n"):
        if line.startswith("diff --git a/"):
            file_path = line.removeprefix("diff --git a/").split(" ", 1)[0]
            for keyword, pass_name in _SOURCE_KEYWORDS:
                if file_path.startswith(keyword):
                    return pass_name
    for line in patch_text.split("\n"):
        if line.startswith("diff --git a/"):
            file_path = line.removeprefix("diff --git a/").split(" ", 1)[0]
            for keyword, pass_name in _O3_KEYWORD:
                if file_path.startswith(keyword):
                    return pass_name
    return None


def is_relevant_pr_file(pr_file_path: str) -> bool:
    return any(pr_file_path.startswith(keyword) for keyword, _pass_name in ALL_KEYWORDS)


def is_test_file(pr_file_path: str) -> bool:
    return any(
        pr_file_path.startswith(keyword)
        for keyword, _pass_name in _TEST_KEYWORDS + _O3_KEYWORD
    )


def is_source_file(pr_file_path: str) -> bool:
    return any(
        pr_file_path.startswith(keyword) for keyword, _pass_name in _SOURCE_KEYWORDS
    )
