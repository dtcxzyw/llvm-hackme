from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from llvm_hackme.models import BugKind, Reproducer
from llvm_hackme.state import StateStore


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


class TestStateStore:
    def test_create_and_detect_schema(self, state_db: Path) -> None:
        store = StateStore(state_db)
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pull_state'"
        ).fetchone()
        assert row is not None
        store.close()

    def test_get_pull_state_returns_default_for_unknown(self, state_db: Path) -> None:
        store = StateStore(state_db)
        result = store.get_pull_state(42)
        assert result.pr_number == 42
        assert result.head_sha is None
        assert result.patch_sha256 is None
        assert result.comment_id is None
        assert result.comment_url is None
        assert result.reproducer is None
        store.close()

    def test_record_pr_update(self, state_db: Path) -> None:
        store = StateStore(state_db)
        store.record_pr_update(42, head_sha="abc123", patch_sha256="sha256def")
        result = store.get_pull_state(42)
        assert result.head_sha == "abc123"
        assert result.patch_sha256 == "sha256def"
        store.close()

    def test_record_pr_update_overwrites(self, state_db: Path) -> None:
        store = StateStore(state_db)
        store.record_pr_update(42, head_sha="abc123", patch_sha256="sha256def")
        store.record_pr_update(42, head_sha="newsha", patch_sha256="newsha256")
        result = store.get_pull_state(42)
        assert result.head_sha == "newsha"
        assert result.patch_sha256 == "newsha256"
        store.close()

    def test_save_comment(self, state_db: Path) -> None:
        store = StateStore(state_db)
        store.save_comment(42, comment_id=123, comment_url="https://example.com/1")
        result = store.get_pull_state(42)
        assert result.comment_id == 123
        assert result.comment_url == "https://example.com/1"
        store.close()

    def test_save_and_retrieve_reproducer(self, state_db: Path) -> None:
        store = StateStore(state_db)
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("/tmp/test.ll"),
            command=["opt", "test.ll"],
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
            stacktrace="SIGSEGV at 0x0",
        )
        store.save_reproducer(42, reproducer)
        result = store.get_pull_state(42)
        assert result.reproducer is not None
        assert result.reproducer.kind == BugKind.CRASH
        assert result.reproducer.source_path == Path("/tmp/test.ll")
        assert result.reproducer.baseline_revision == "rev123"
        assert result.reproducer.pr_head_sha == "sha456"
        assert result.reproducer.patch_sha256 == "sha256abc"
        assert result.reproducer.stacktrace == "SIGSEGV at 0x0"
        assert result.reproducer.alive2_counterexample is None
        store.close()

    def test_save_and_retrieve_miscompilation_reproducer(self, state_db: Path) -> None:
        store = StateStore(state_db)
        reproducer = Reproducer(
            kind=BugKind.MISCOMPILATION,
            source_path=Path("/tmp/test.ll"),
            command=["opt", "test.ll"],
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
            alive2_counterexample="Transformation doesn't verify!",
        )
        store.save_reproducer(42, reproducer)
        result = store.get_pull_state(42)
        assert result.reproducer is not None
        assert result.reproducer.kind == BugKind.MISCOMPILATION
        assert result.reproducer.stacktrace is None
        assert (
            result.reproducer.alive2_counterexample == "Transformation doesn't verify!"
        )
        store.close()

    def test_clear_reproducer(self, state_db: Path) -> None:
        store = StateStore(state_db)
        reproducer = Reproducer(
            kind=BugKind.CRASH,
            source_path=Path("/tmp/test.ll"),
            command=["opt", "test.ll"],
            baseline_revision="rev123",
            pr_head_sha="sha456",
            patch_sha256="sha256abc",
            stacktrace="crash",
        )
        store.save_reproducer(42, reproducer)
        store.clear_reproducer(42)
        result = store.get_pull_state(42)
        assert result.reproducer is None
        store.close()

    def test_scan_watermark(self, state_db: Path) -> None:
        store = StateStore(state_db)
        assert store.get_scan_watermark() is None
        dt = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        store.set_scan_watermark(dt)
        retrieved = store.get_scan_watermark()
        assert retrieved is not None
        assert retrieved.isoformat() == "2024-01-01T00:00:00+00:00"
        store.close()
