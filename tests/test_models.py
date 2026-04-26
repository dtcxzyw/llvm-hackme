from __future__ import annotations

from pathlib import Path

from llvm_hackme.models import BugKind, Reproducer


class TestModels:
    def test_reproducer_to_json_crash(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("/tmp/test.ll"),
            command=["opt", "test.ll"],
            baseline_revision="rev1",
            pr_head_sha="sha1",
            patch_sha256="sha256abc",
            stacktrace="SIGSEGV",
        )
        payload = reproducer.to_json()
        assert payload["kind"] == "crash"
        assert payload["command"] == ["opt", "test.ll"]
        assert payload["baseline_revision"] == "rev1"
        assert payload["pr_head_sha"] == "sha1"
        assert payload["patch_sha256"] == "sha256abc"
        assert payload["stacktrace"] == "SIGSEGV"
        assert payload["alive2_counterexample"] is None

    def test_reproducer_to_json_miscompilation(self) -> None:
        reproducer = Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=Path("/tmp/test.ll"),
            command=["opt"],
            baseline_revision="rev1",
            pr_head_sha="sha1",
            patch_sha256="sha256abc",
            alive2_counterexample="ERROR: incorrect",
        )
        payload = reproducer.to_json()
        assert payload["kind"] == "miscompilation"
        assert payload["stacktrace"] is None
        assert payload["alive2_counterexample"] == "ERROR: incorrect"

    def test_reproducer_from_json_crash(self) -> None:
        payload = {
            "kind": "crash",
            "source_path": "/tmp/test.ll",
            "command": ["opt"],
            "baseline_revision": "rev1",
            "pr_head_sha": "sha1",
            "patch_sha256": "p",
            "stacktrace": "crash",
        }
        reproducer = Reproducer.from_json(payload)
        assert reproducer.kind == BugKind.CRASH
        assert reproducer.stacktrace == "crash"
        assert reproducer.alive2_counterexample is None

    def test_reproducer_from_json_miscompilation(self) -> None:
        payload = {
            "kind": "miscompilation",
            "source_path": "/tmp/test.ll",
            "command": ["opt"],
            "baseline_revision": "rev1",
            "pr_head_sha": "sha1",
            "patch_sha256": "p",
            "alive2_counterexample": "ERROR",
        }
        reproducer = Reproducer.from_json(payload)
        assert reproducer.kind == BugKind.MISCOMPILATION
        assert reproducer.alive2_counterexample == "ERROR"
        assert reproducer.stacktrace is None

    def test_reproducer_roundtrip(self) -> None:
        original = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("/path/in.ll"),
            command=["opt", "in.ll"],
            baseline_revision="r",
            pr_head_sha="s",
            patch_sha256="p",
            stacktrace="trace",
        )
        restored = Reproducer.from_json(original.to_json())
        assert restored.kind == original.kind
        assert restored.command == original.command
        assert restored.baseline_revision == original.baseline_revision
        assert restored.pr_head_sha == original.pr_head_sha
        assert restored.patch_sha256 == original.patch_sha256
        assert restored.stacktrace == original.stacktrace

    def test_bug_kind_values(self) -> None:
        assert BugKind.CRASH.value == "crash"
        assert BugKind.MISCOMPILATION.value == "miscompilation"

    def test_comment_state_values(self) -> None:
        from llvm_hackme.models import CommentState

        assert CommentState.BUG_FOUND.value == "bug_found"
        assert CommentState.STILL_REPRODUCES.value == "still_reproduces"
        assert (
            CommentState.NO_ISSUE_FOUND_FOR_CURRENT_PATCH.value
            == "no_issue_found_for_current_patch"
        )
