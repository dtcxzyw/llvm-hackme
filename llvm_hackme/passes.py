from __future__ import annotations

PASS_KEYWORDS: list[tuple[str, str]] = [
    ("llvm/lib/Transforms/InstCombine", "instcombine<no-verify-fixpoint>"),
    ("llvm/test/Transforms/InstCombine", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Transforms/InstSimplify", "instcombine<no-verify-fixpoint>"),
    ("llvm/test/Transforms/InstSimplify", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Analysis/InstructionSimplify", "instcombine<no-verify-fixpoint>"),
    ("llvm/lib/Analysis/ValueTracking", "instcombine<no-verify-fixpoint>"),
    ("llvm/test/Analysis/ValueTracking", "instcombine<no-verify-fixpoint>"),
    (
        "llvm/lib/Transforms/ConstraintElimination",
        "constraint-elimination",
    ),
    (
        "llvm/test/Transforms/ConstraintElimination",
        "constraint-elimination",
    ),
    ("llvm/lib/Transforms/Scalar/EarlyCSE", "early-cse"),
    ("llvm/test/Transforms/EarlyCSE", "early-cse"),
    ("llvm/lib/Transforms/Scalar/GVN", "gvn"),
    ("llvm/test/Transforms/GVN", "gvn"),
    ("llvm/lib/Transforms/Scalar/NewGVN", "newgvn"),
    ("llvm/test/Transforms/NewGVN", "newgvn"),
    ("llvm/lib/Transforms/Scalar/Reassociate", "reassociate"),
    ("llvm/test/Transforms/Reassociate", "reassociate"),
    ("llvm/lib/Transforms/Scalar/SCCP", "sccp"),
    ("llvm/test/Transforms/SCCP", "sccp"),
    (
        "llvm/lib/Transforms/Scalar/CorrelatedValuePropagation",
        "correlated-propagation",
    ),
    (
        "llvm/test/Transforms/CorrelatedValuePropagation",
        "correlated-propagation",
    ),
    ("llvm/lib/Transforms/Utils/SimplifyCFG.cpp", "simplifycfg"),
    ("llvm/test/Transforms/SimplifyCFG", "simplifycfg"),
    ("llvm/lib/Transforms/Vectorize/VectorCombine", "vector-combine"),
    ("llvm/test/Transforms/VectorCombine", "vector-combine"),
    (
        "llvm/lib/Transforms/AggressiveInstCombine",
        "aggressive-instcombine",
    ),
    (
        "llvm/test/Transforms/AggressiveInstCombine",
        "aggressive-instcombine",
    ),
    ("llvm/test/Transforms/PhaseOrdering", "default<O3>"),
]


def guess_pass_name(patch_text: str) -> str | None:
    for line in patch_text.split("\n"):
        if line.startswith("diff --git a/"):
            file_path = line.removeprefix("diff --git a/").split(" ", 1)[0]
            for keyword, pass_name in PASS_KEYWORDS:
                if file_path.startswith(keyword):
                    return pass_name
    return None


def is_relevant_pr_file(pr_file_path: str) -> bool:
    for keyword, _pass_name in PASS_KEYWORDS:
        if pr_file_path.startswith(keyword):
            return True
    return False
