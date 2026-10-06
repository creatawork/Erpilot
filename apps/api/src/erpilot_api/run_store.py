"""SQLite run checkpoints owned by the API composition layer (ADR-0008)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1


class ActiveRunError(RuntimeError):
    """A session already has a run that has not ended."""


class SchemaVersionError(RuntimeError):
    """Stored run data needs an explicit migration before use."""


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='run_session'"
            ).fetchone()
            if version not in (0, SCHEMA_VERSION) or (version == 0 and existing):
                raise SchemaVersionError(f"unsupported run schema version: {version}")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS run_session (
                    session_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    history_json TEXT NOT NULL,
                    active_run_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS run (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES run_session(session_id),
                    user_input TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checkpoint_version INTEGER NOT NULL,
                    messages_json TEXT NOT NULL,
                    final_answer TEXT,
                    error TEXT,
                    trace_path TEXT,
                    execution_owner TEXT,
                    lease_until TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
                    ON run(session_id)
                    WHERE status IN ('running', 'waiting_approval', 'recovering');
            """)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_run(
        self, session_id: str, user_input: str, history: list, trace_path: str | None
    ) -> str:
        run_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT schema_version FROM run_session WHERE session_id=?", (session_id,)
            ).fetchone()
            if session and session[0] != SCHEMA_VERSION:
                raise SchemaVersionError(f"unsupported session schema version: {session[0]}")
            if session is None:
                conn.execute(
                    "INSERT INTO run_session(session_id, schema_version, history_json) "
                    "VALUES (?, ?, ?)",
                    (session_id, SCHEMA_VERSION, _json(history)),
                )
            try:
                conn.execute(
                    "INSERT INTO run(run_id, session_id, user_input, status, checkpoint_version, "
                    "messages_json, trace_path) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                    (
                        run_id,
                        session_id,
                        user_input,
                        SCHEMA_VERSION,
                        _json([*history, {"role": "user", "content": user_input}]),
                        trace_path,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ActiveRunError(f"session {session_id} already has an active run") from exc
            conn.execute(
                "UPDATE run_session SET active_run_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=?",
                (run_id, session_id),
            )
        return run_id

    def checkpoint_run(self, run_id: str, messages: list) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE run SET messages_json=?, revision=revision+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status='running'",
                (_json(messages), run_id),
            )

    def finish_run(self, run_id: str, messages: list, final_answer: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT session_id FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            conn.execute(
                "UPDATE run SET status='completed', messages_json=?, final_answer=?, "
                "revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (_json(messages), final_answer, run_id),
            )
            conn.execute(
                "UPDATE run_session SET history_json=?, active_run_id=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
                (_json(messages), row[0]),
            )

    def project_run(self, run_id: str, messages: list, status: str) -> None:
        """Display projection only; graph checkpoints remain the execution authority."""
        if status not in ("running", "waiting_approval"):
            raise ValueError("unsupported projection status")
        with self._connect() as conn:
            conn.execute(
                "UPDATE run SET messages_json=?, status=?, revision=revision+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (_json(messages), status, run_id),
            )

    def fail_run(self, run_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT session_id FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            conn.execute(
                "UPDATE run SET status='failed', error=?, revision=revision+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (error, run_id),
            )
            conn.execute(
                "UPDATE run_session SET active_run_id=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=?",
                (row[0],),
            )

    def update_session_history(self, session_id: str, messages: list) -> None:
        """Refresh the completed-history projection after continuing a checkpoint."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_session SET history_json=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=?",
                (_json(messages), session_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["checkpoint_version"] != SCHEMA_VERSION:
            raise SchemaVersionError("unsupported run checkpoint version")
        result["messages"] = json.loads(result.pop("messages_json"))
        return result

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_session WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["schema_version"] != SCHEMA_VERSION:
            raise SchemaVersionError("unsupported session schema version")
        result["history"] = json.loads(result.pop("history_json"))
        return result


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
