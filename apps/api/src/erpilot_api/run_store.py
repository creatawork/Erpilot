"""SQLite run checkpoints owned by the API composition layer (ADR-0008)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
PRESENTATION_SCHEMA_VERSION = 1
PRESENTATION_RETENTION_DAYS = 30


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
            # 展示事件表（设计 5.3）：UI 投影数据，不替代 LangGraph checkpoint，
            # 不参与写操作决策。event_id 供客户端去重；seq 在运行内单调递增。
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS presentation_event (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    schema_version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS presentation_by_session
                    ON presentation_event(session_id, seq);
            """)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.prune_presentation()

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

    def resume_run(self, session_id: str) -> str | None:
        """Reattach the latest interrupted projection after checkpoint validation."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT run_id, status FROM run WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None or row["status"] == "completed":
                return None
            conn.execute(
                "UPDATE run SET status='running', error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=?", (row["run_id"],),
            )
            conn.execute(
                "UPDATE run_session SET active_run_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=?", (row["run_id"], session_id),
            )
            return row["run_id"]

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

    # ---- 展示事件（设计 5.3）：只投影排版与可证明的过程 ----

    def next_presentation_seq(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM presentation_event WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return int(row[0]) + 1

    def append_presentation_events(self, events: list[dict[str, Any]]) -> None:
        """批量写入展示事件；event_id 冲突忽略（重放/恢复段去重靠它）。"""
        if not events:
            return
        rows = [
            (
                event["event_id"],
                event["session_id"],
                event["run_id"],
                event["segment_id"],
                event["seq"],
                event["type"],
                _json(event["payload"]),
                PRESENTATION_SCHEMA_VERSION,
            )
            for event in events
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO presentation_event"
                "(event_id, session_id, run_id, segment_id, seq, type, payload, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def read_presentation_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, run_id, segment_id, seq, type, payload, occurred_at "
                "FROM presentation_event WHERE session_id=? ORDER BY rowid",
                (session_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "segment_id": row["segment_id"],
                "seq": row["seq"],
                "type": row["type"],
                "payload": json.loads(row["payload"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    def prune_presentation(self, days: int = PRESENTATION_RETENTION_DAYS) -> None:
        """默认保留 30 天；历史 messages 仍可展示，展示日志只服务近期恢复。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM presentation_event "
                "WHERE occurred_at < datetime('now', ?)",
                (f"-{days} days",),
            )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
