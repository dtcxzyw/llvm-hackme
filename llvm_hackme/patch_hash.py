from __future__ import annotations

import hashlib
import logging

from unidiff import PatchSet

LOGGER = logging.getLogger(__name__)

_IGNORED_PATH_PREFIXES = ("llvm/test/", "llvm/unittests/")


def _strip_patch_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _is_relevant_path(path: str) -> bool:
    if path == "/dev/null" or not path.startswith("llvm/"):
        return False
    return not path.startswith(_IGNORED_PATH_PREFIXES)


def _split_segments(patch: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                segments.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        segments.append("".join(current))
    return segments


def _relevant_segments(patch: str) -> list[str]:
    segments = _split_segments(patch)
    try:
        parsed = PatchSet(patch)
    except Exception:
        LOGGER.warning(
            "Failed to parse patch with unidiff, hashing full patch",
            exc_info=True,
        )
        return segments
    if len(parsed) != len(segments):
        LOGGER.warning(
            "Patch parse mismatch (%d files vs %d segments), hashing full patch",
            len(parsed),
            len(segments),
        )
        return segments
    relevant: list[str] = []
    for file, segment in zip(parsed, segments, strict=True):
        old_path = _strip_patch_prefix(file.source_file)
        new_path = _strip_patch_prefix(file.path)
        if _is_relevant_path(old_path) or _is_relevant_path(new_path):
            relevant.append(segment)
    return relevant


def compute_patch_hash(patch: str) -> str:
    """SHA-256 of a patch restricted to relevant llvm/ source changes.

    Changes outside llvm/ (e.g. clang/, mlir/), under llvm/test/ and
    llvm/unittests/ are ignored, so review decisions only depend on the
    source code changes that can actually affect the fuzzed toolchain.
    """
    relevant = _relevant_segments(patch)
    return hashlib.sha256("".join(relevant).encode("utf-8")).hexdigest()
