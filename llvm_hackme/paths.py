from __future__ import annotations

INSTCOMBINE_PREFIX = "llvm/lib/Transforms/InstCombine/"


def is_relevant_pr_file(path: str) -> bool:
    return path.startswith(INSTCOMBINE_PREFIX)
