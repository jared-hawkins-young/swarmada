"""SQLite-backed durable store for drift-detection.

Three logical tables:
- diagnosis: retrieval by (robot_id, fleet_action_id)
- window_snapshot: crash-recovery for in-memory rolling windows
- dedup_seen: dedup guard for fleet_action_id within retention window

Schema mirrors spec/data-model.md.

Fail-closed posture: all write paths raise on SQLite errors; the caller
decides whether to retry, abort, or degrade.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS diagnosis (
    robot_id           TEXT NOT NULL,
    fleet_action_id    TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    model_used         TEXT NOT NULL,
    langfuse_trace_id  TEXT NOT NULL,
    PRIMARY KEY (robot_id, fleet_action_id)
);

CREATE INDEX IF NOT EXISTS ix_diagnosis_created_at ON diagnosis(created_at);

CREATE TABLE IF NOT EXISTS window_snapshot (
    robot_id        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    snapshotted_at  TEXT NOT NULL,
    PRIMARY KEY (robot_id, model_id, model_version, task_type)
);

CREATE TABLE IF NOT EXISTS dedup_seen (
    fleet_action_id  TEXT PRIMARY KEY,
    seen_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_dedup_seen_at ON dedup_seen(seen_at);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DriftStore:
    """Thread-safe wrapper around a SQLite database file for drift diagnoses."""

    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so async tasks can dispatch writes from
        # any thread; we serialize with the connection's implicit lock.
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_SQL)

    # --- diagnosis ---

    def insert_diagnosis(
        self,
        *,
        robot_id: str,
        fleet_action_id: str,
        payload: dict[str, Any],
        prompt_version: str,
        model_used: str,
        langfuse_trace_id: str,
    ) -> None:
        """Persist a diagnosis. Idempotent on (robot_id, fleet_action_id)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO diagnosis
                (robot_id, fleet_action_id, payload_json, created_at,
                 prompt_version, model_used, langfuse_trace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                robot_id,
                fleet_action_id,
                json.dumps(payload, sort_keys=True),
                _now_iso(),
                prompt_version,
                model_used,
                langfuse_trace_id,
            ),
        )

    def get_diagnosis(
        self,
        *,
        robot_id: str,
        fleet_action_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve a diagnosis by correlation IDs. Returns None if absent."""
        cur = self._conn.execute(
            """
            SELECT payload_json, created_at, prompt_version, model_used, langfuse_trace_id
            FROM diagnosis
            WHERE robot_id = ? AND fleet_action_id = ?
            """,
            (robot_id, fleet_action_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        payload_json, created_at, prompt_version, model_used, langfuse_trace_id = row
        payload = json.loads(payload_json)
        payload.setdefault("created_at", created_at)
        payload.setdefault("prompt_version", prompt_version)
        payload.setdefault("model_used", model_used)
        payload.setdefault("langfuse_trace_id", langfuse_trace_id)
        return payload

    def prune_diagnoses_older_than(self, days: int) -> int:
        """Delete diagnoses older than `days`. Returns count removed."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM diagnosis WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def diagnosis_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM diagnosis")
        (count,) = cur.fetchone()
        return int(count)

    # --- window snapshot ---

    def upsert_window_snapshot(
        self,
        *,
        robot_id: str,
        model_id: str,
        model_version: str,
        task_type: str,
        snapshot: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO window_snapshot
                (robot_id, model_id, model_version, task_type,
                 snapshot_json, snapshotted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                robot_id,
                model_id,
                model_version,
                task_type,
                json.dumps(snapshot, sort_keys=True),
                _now_iso(),
            ),
        )

    def load_all_window_snapshots(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT robot_id, model_id, model_version, task_type,
                   snapshot_json, snapshotted_at
            FROM window_snapshot
            """
        )
        out: list[dict[str, Any]] = []
        for robot_id, model_id, model_version, task_type, snap_json, at in cur:
            snap = json.loads(snap_json)
            snap["_key"] = {
                "robot_id": robot_id,
                "model_id": model_id,
                "model_version": model_version,
                "task_type": task_type,
            }
            snap["_snapshotted_at"] = at
            out.append(snap)
        return out

    # --- dedup ---

    def mark_seen(self, fleet_action_id: str) -> bool:
        """Record fleet_action_id. Returns True if newly recorded, False if
        already present (i.e. this is a duplicate submission)."""
        try:
            self._conn.execute(
                "INSERT INTO dedup_seen (fleet_action_id, seen_at) VALUES (?, ?)",
                (fleet_action_id, _now_iso()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def prune_dedup_older_than(self, hours: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM dedup_seen WHERE seen_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    # --- lifecycle ---

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DriftStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
