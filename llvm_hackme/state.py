from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from llvm_hackme.models import Reproducer


@dataclass(frozen=True)
class StoredPullState:
    pr_number: int
    head_sha: str | None
    patch_sha256: str | None
    comment_id: int | None
    comment_url: str | None
    reproducer: Reproducer | None
    processed_at: datetime | None


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pull_state (
                    pr_number INTEGER PRIMARY KEY,
                    head_sha TEXT,
                    patch_sha256 TEXT,
                    comment_id INTEGER,
                    comment_url TEXT,
                    reproducer_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    "ALTER TABLE pull_state ADD COLUMN processed_at TEXT"
                )

    def get_scan_watermark(self) -> datetime | None:
        value = self._get_metadata("scan_watermark")
        return datetime.fromisoformat(value) if value else None

    def set_scan_watermark(self, watermark: datetime) -> None:
        self._set_metadata("scan_watermark", watermark.isoformat())

    def get_pull_state(self, pr_number: int) -> StoredPullState:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pull_state WHERE pr_number = ?", (pr_number,)
            ).fetchone()
        if row is None:
            return StoredPullState(
                pr_number=pr_number,
                head_sha=None,
                patch_sha256=None,
                comment_id=None,
                comment_url=None,
                reproducer=None,
                processed_at=None,
            )
        processed_at = None
        raw_processed = (
            row["processed_at"] if "processed_at" in row.keys() else None  # noqa: SIM118
        )
        if raw_processed:
            processed_at = datetime.fromisoformat(raw_processed)
        return StoredPullState(
            pr_number=pr_number,
            head_sha=row["head_sha"],
            patch_sha256=row["patch_sha256"],
            comment_id=row["comment_id"],
            comment_url=row["comment_url"],
            reproducer=_decode_reproducer(row["reproducer_json"]),
            processed_at=processed_at,
        )

    def record_pr_update(
        self, pr_number: int, *, head_sha: str, patch_sha256: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO pull_state (
                    pr_number,
                    head_sha,
                    patch_sha256,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pr_number) DO UPDATE SET
                    head_sha = excluded.head_sha,
                    patch_sha256 = excluded.patch_sha256,
                    updated_at = excluded.updated_at
                """,
                (pr_number, head_sha, patch_sha256, datetime.utcnow().isoformat()),
            )

    def save_comment(self, pr_number: int, comment_id: int, comment_url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO pull_state (
                    pr_number,
                    comment_id,
                    comment_url,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pr_number) DO UPDATE SET
                    comment_id = excluded.comment_id,
                    comment_url = excluded.comment_url,
                    updated_at = excluded.updated_at
                """,
                (pr_number, comment_id, comment_url, datetime.utcnow().isoformat()),
            )

    def save_reproducer(self, pr_number: int, reproducer: Reproducer) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO pull_state (
                    pr_number,
                    reproducer_json,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(pr_number) DO UPDATE SET
                    reproducer_json = excluded.reproducer_json,
                    updated_at = excluded.updated_at
                """,
                (
                    pr_number,
                    json.dumps(reproducer.to_json(), sort_keys=True),
                    datetime.utcnow().isoformat(),
                ),
            )

    def clear_reproducer(self, pr_number: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE pull_state
                SET reproducer_json = NULL, updated_at = ?
                WHERE pr_number = ?
                """,
                (datetime.utcnow().isoformat(), pr_number),
            )

    def mark_processed(self, pr_number: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE pull_state
                SET processed_at = ?, updated_at = ?
                WHERE pr_number = ?
                """,
                (
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                    pr_number,
                ),
            )

    def _get_metadata(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_metadata(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )


def _decode_reproducer(value: str | None) -> Reproducer | None:
    if not value:
        return None
    try:
        return Reproducer.from_json(json.loads(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
