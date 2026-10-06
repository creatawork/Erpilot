"""SQLite run checkpoints owned by the API composition layer (ADR-0008)."""

import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1

# run 的活动状态（部分唯一索引的口径；结束态 completed/failed/cancelled 不在内）
ACTIVE_RUN_STATUSES = ("running", "waiting_approval", "recovering")

# invocation 的终态：恢复协调不再改写
TERMINAL_INVOCATION_STATUSES = ("succeeded", "denied", "failed")


class ActiveRunError(RuntimeError):
    """A session already has a run that has not ended."""


class SchemaVersionError(RuntimeError):
    """Stored run data needs an explicit migration before use."""


class WriteIntentConflictError(ValueError):
    """A persisted token cannot be reused for a different invocation identity."""


class ApprovalStateError(ValueError):
    def __init__(self, status_code: int, message: str, approval: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.approval = approval


class RunStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(UTC))
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
                CREATE TABLE IF NOT EXISTS invocation (
                    invocation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES run(run_id),
                    call_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    tool_schema_version TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_fingerprint TEXT NOT NULL,
                    client_token TEXT NOT NULL UNIQUE,
                    approval_pending_id TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS approval (
                    pending_id TEXT PRIMARY KEY,
                    invocation_id TEXT NOT NULL UNIQUE REFERENCES invocation(invocation_id),
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    arguments_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_version INTEGER NOT NULL DEFAULT 1,
                    decided_reason TEXT,
                    decided_at TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS invocation_by_run ON invocation(run_id);
                CREATE TABLE IF NOT EXISTS run_event (
                    run_id TEXT NOT NULL REFERENCES run(run_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (run_id, seq)
                );
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
                    (run_id, session_id, user_input, SCHEMA_VERSION,
                     _json([*history, {"role": "user", "content": user_input}]), trace_path),
                )
            except sqlite3.IntegrityError as exc:
                raise ActiveRunError(f"session {session_id} already has an active run") from exc
            conn.execute(
                "UPDATE run_session SET active_run_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=?", (run_id, session_id),
            )
        return run_id

    def checkpoint_run(self, run_id: str, messages: list) -> None:
        placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE run SET messages_json=?, revision=revision+1, "
                f"updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND status IN ({placeholders})",
                (_json(messages), run_id, *ACTIVE_RUN_STATUSES),
            )

    def finish_run(self, run_id: str, messages: list, final_answer: str) -> bool:
        """只有所有写调用已确定才收口；否则保留检查点及活动占用用于对账。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT session_id FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            unresolved = conn.execute(
                f"SELECT 1 FROM invocation WHERE run_id=? AND status NOT IN "
                f"({_placeholders(TERMINAL_INVOCATION_STATUSES)}) LIMIT 1",
                (run_id, *TERMINAL_INVOCATION_STATUSES),
            ).fetchone()
            if unresolved:
                conn.execute(
                    "UPDATE run SET status='recovering', messages_json=?, final_answer=NULL, "
                    "revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (_json(messages), run_id),
                )
                return False
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
        return True

    def fail_run(self, run_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT session_id,status FROM run WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] == "cancelled":
                return
            uncertain = conn.execute(
                "SELECT 1 FROM invocation WHERE run_id=? "
                "AND status IN ('unknown', 'executing') LIMIT 1", (run_id,),
            ).fetchone()
            status = "recovering" if uncertain else "failed"
            conn.execute(
                "UPDATE run SET status=?, error=?, revision=revision+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (status, error, run_id),
            )
            if not uncertain:
                conn.execute(
                    "UPDATE run_session SET active_run_id=NULL, updated_at=CURRENT_TIMESTAMP "
                    "WHERE session_id=?", (row[0],),
                )

    def set_run_status(self, run_id: str, status: str) -> None:
        """活动状态间推进（recovering/waiting_approval 等）；结束态走 finish/fail。"""
        placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE run SET status=?, updated_at=CURRENT_TIMESTAMP "
                f"WHERE run_id=? AND status IN ({placeholders})",
                (status, run_id, *ACTIVE_RUN_STATUSES),
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


    # ---- 写调用身份与审批（T05，ADR-0008 §2/§3：W1 在展示前同事务落盘） ----

    def record_write_intent(
        self,
        run_id: str,
        call_id: str,
        *,
        session_id: str,
        tool: str,
        tool_schema_version: str,
        arguments: dict,
        client_token: str,
        pending_id: str,
        ttl_seconds: int,
    ) -> str:
        """在审批展示前固化一次写调用的身份：token、call_id、规范化参数、审批。

        invocation 与 approval(pending) 在**同一事务**提交，run 随之进入
        waiting_approval——消费方看到审批卡片时数据已可跨重启重建（W1/R01）。
        arguments 剥离 client_token 后按 mutations 的规范化口径落盘，
        指纹绑定业务参数（批准不可迁移到异参动作）。
        """
        if not client_token:
            raise ValueError("client_token 不能为空：写意图必须带服务端生成的稳定幂等键")
        clean_args = {k: v for k, v in arguments.items() if k != "client_token"}
        arguments_json = _json(clean_args)
        fingerprint = compute_fingerprint(tool, tool_schema_version, clean_args)
        expires_at = self._clock() + timedelta(seconds=ttl_seconds)
        invocation_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT session_id FROM run WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run {run_id} 不存在，写意图无处归属")
            if row[0] != session_id:
                raise ValueError("写意图归属校验失败：run 不属于该 session")
            # 幂等（T08 事件补发的 ApprovalPending 重放不会撞唯一约束）：
            # 仅完全一致的身份可以重放；异参/异归属不得沿用旧审批。
            existing = conn.execute(
                "SELECT i.*, a.session_id AS approval_session_id, "
                "a.run_id AS approval_run_id, a.arguments_fingerprint AS approval_fingerprint "
                "FROM invocation i LEFT JOIN approval a ON a.pending_id=i.approval_pending_id "
                "AND a.invocation_id=i.invocation_id WHERE i.client_token=?",
                (client_token,),
            ).fetchone()
            if existing is not None:
                identity = {
                    "run_id": run_id, "call_id": call_id, "tool": tool,
                    "tool_schema_version": tool_schema_version, "arguments_json": arguments_json,
                    "arguments_fingerprint": fingerprint, "approval_pending_id": pending_id,
                    "approval_session_id": session_id, "approval_run_id": run_id,
                    "approval_fingerprint": fingerprint,
                }
                if any(existing[key] != value for key, value in identity.items()):
                    raise WriteIntentConflictError(
                        "client_token 已绑定不同写调用身份或参数，不能继承原审批"
                    )
                return existing["invocation_id"]
            conn.execute(
                f"UPDATE run SET status='waiting_approval', updated_at=CURRENT_TIMESTAMP "
                f"WHERE run_id=? AND status IN ({_placeholders(ACTIVE_RUN_STATUSES)})",
                (run_id, *ACTIVE_RUN_STATUSES),
            )
            conn.execute(
                "INSERT INTO invocation(invocation_id, run_id, call_id, tool, "
                "tool_schema_version, arguments_json, arguments_fingerprint, client_token, "
                "approval_pending_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'waiting_approval')",
                (invocation_id, run_id, call_id, tool, tool_schema_version,
                 arguments_json, fingerprint, client_token, pending_id),
            )
            conn.execute(
                "INSERT INTO approval(pending_id, invocation_id, run_id, session_id, "
                "arguments_fingerprint, status, expected_version, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 1, ?)",
                (pending_id, invocation_id, run_id, session_id, fingerprint,
                 expires_at.isoformat()),
            )
        return invocation_id

    def record_approval_decision(
        self,
        pending_id: str,
        approved: bool,
        reason: str = "",
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """条件更新落一次审批决定；只能从 pending 决策一次（R07 的持久化面）。

        expected_version 给定时一并校验乐观锁版本。返回决定后的审批行；
        pending_id 未知、已决或版本竞争返回 None（调用方按已决语义处理）。
        """
        condition = " AND expected_version=?" if expected_version is not None else ""
        params: list[Any] = [pending_id]
        if expected_version is not None:
            params.append(expected_version)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT invocation_id FROM approval "
                f"WHERE pending_id=? AND status='pending'{condition}", params,
            ).fetchone()
            if row is None:
                return None
            invocation_id = row[0]
            status = "approved" if approved else "denied"
            conn.execute(
                "UPDATE approval SET status=?, decided_reason=?, decided_at=?, "
                "expected_version=expected_version+1 WHERE pending_id=?",
                (status, reason, _utc_now().isoformat(), pending_id),
            )
            conn.execute(
                "UPDATE invocation SET status=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE invocation_id=?", (status, invocation_id),
            )
        return self.get_approval(pending_id)

    def decide_approval(
        self, pending_id: str, approved: bool, reason: str = "", *,
        run_id: str | None = None, session_id: str | None = None,
        expected_version: int | None = None, arguments_fingerprint: str | None = None,
    ) -> dict:
        expired = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM approval WHERE pending_id=?", (pending_id,),
            ).fetchone()
            if row is None:
                raise ApprovalStateError(404, "审批不存在")
            approval = dict(row)
            inv = conn.execute(
                "SELECT * FROM invocation WHERE invocation_id=?", (row["invocation_id"],),
            ).fetchone()
            run = conn.execute("SELECT * FROM run WHERE run_id=?", (row["run_id"],)).fetchone()
            fingerprint = compute_fingerprint(
                inv["tool"], inv["tool_schema_version"], json.loads(inv["arguments_json"]),
            ) if inv else None
            if (
                not run or not inv or inv["run_id"] != row["run_id"]
                or run["session_id"] != row["session_id"]
                or inv["approval_pending_id"] != pending_id
                or fingerprint != row["arguments_fingerprint"]
                or fingerprint != inv["arguments_fingerprint"]
                or (run_id is not None and run_id != row["run_id"])
                or (session_id is not None and session_id != row["session_id"])
                or (arguments_fingerprint is not None and arguments_fingerprint != fingerprint)
            ):
                raise ApprovalStateError(409, "审批归属或参数指纹冲突", approval)
            desired = "approved" if approved else "denied"
            if row["status"] == desired:
                if expected_version is not None and expected_version not in (
                    row["expected_version"], row["expected_version"] - 1,
                ):
                    raise ApprovalStateError(409, "审批版本冲突", approval)
                return approval
            if row["status"] == "expired":
                raise ApprovalStateError(410, "审批已过期", approval)
            if row["status"] != "pending" or run["status"] not in ACTIVE_RUN_STATUSES:
                raise ApprovalStateError(409, "审批已决定或任务已取消", approval)
            if expected_version is not None and expected_version != row["expected_version"]:
                raise ApprovalStateError(409, "审批版本冲突", approval)
            expired = datetime.fromisoformat(row["expires_at"]) <= self._clock()
            status = "expired" if expired else desired
            conn.execute(
                "UPDATE approval SET status=?, decided_reason=?, decided_at=?, "
                "expected_version=expected_version+1 WHERE pending_id=? AND status='pending' "
                "AND expected_version=?",
                (status, reason, self._clock().isoformat(), pending_id, row["expected_version"]),
            )
            conn.execute(
                "UPDATE invocation SET status=?, result_json=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE invocation_id=?",
                ("failed" if expired else desired,
                 _json({"error": {"type": "approval_expired"}}) if expired else (
                     _json({"approval": "denied", "reason": reason}) if not approved else None
                 ), row["invocation_id"]),
            )
            result = dict(conn.execute(
                "SELECT * FROM approval WHERE pending_id=?", (pending_id,),
            ).fetchone())
        if expired:
            raise ApprovalStateError(410, "审批已过期", result)
        return result

    def expire_approvals(self, run_id: str) -> None:
        for approval in self.list_pending_approvals(run_id):
            if datetime.fromisoformat(approval["expires_at"]) <= self._clock():
                with contextlib.suppress(ApprovalStateError):
                    self.decide_approval(approval["pending_id"], False, "审批已过期")

    def cancel_run(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in ACTIVE_RUN_STATUSES:
                return
            conn.execute(
                "UPDATE approval SET status='cancelled', expected_version=expected_version+1 "
                "WHERE run_id=? AND status='pending'", (run_id,),
            )
            conn.execute(
                "UPDATE invocation SET status='denied', result_json=? WHERE run_id=? "
                "AND status IN ('prepared','waiting_approval','approved')",
                (_json({"approval": "cancelled", "executed": False}), run_id),
            )
            conn.execute("UPDATE run SET status='cancelled', revision=revision+1 WHERE run_id=?",
                         (run_id,))
            conn.execute("UPDATE run_session SET active_run_id=NULL WHERE session_id=?",
                         (run["session_id"],))

    def append_event(self, run_id: str, name: str, payload: dict) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM run_event WHERE run_id=?", (run_id,),
            ).fetchone()[0]
            data = {**payload, "run_id": run_id, "seq": seq}
            conn.execute(
                "INSERT INTO run_event(run_id, seq, event_type, payload_json) VALUES (?,?,?,?)",
                (run_id, seq, name, _json(data)),
            )
        return {"event": name, "data": data}

    def events_after(self, run_id: str, after_seq: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_type,payload_json FROM run_event WHERE run_id=? AND seq>? "
                "ORDER BY seq", (run_id, after_seq),
            ).fetchall()
        return [{"event": row[0], "data": json.loads(row[1])} for row in rows]

    def snapshot(self, run_id: str) -> dict:
        self.expire_approvals(run_id)
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = dict(row)
            result["messages"] = json.loads(result.pop("messages_json"))
            events = conn.execute(
                "SELECT seq,event_type,payload_json FROM run_event WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
            invocations = conn.execute(
                "SELECT * FROM invocation WHERE run_id=? ORDER BY rowid", (run_id,),
            ).fetchall()
            approvals = conn.execute(
                "SELECT * FROM approval WHERE run_id=? ORDER BY rowid", (run_id,),
            ).fetchall()
        result["invocations"] = [self._invocation_dict(row) for row in invocations]
        result["approvals"] = [dict(row) for row in approvals]
        result["pending"] = [a for a in result["approvals"] if a["status"] == "pending"]
        result["last_seq"] = events[-1][0] if events else 0
        text = ""
        for event in events:
            if event[1] == "step":
                text = ""
            if event[1] == "delta":
                text += json.loads(event[2])["text"]
        result["answer_text"] = result["final_answer"] or text
        result["events"] = [{"event": row[1], "data": json.loads(row[2])} for row in events]
        return result

    def list_runs(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id FROM run WHERE session_id=? ORDER BY rowid", (session_id,),
            ).fetchall()
        return [self.snapshot(row[0]) for row in rows]

    def claim_run(self, run_id: str, owner: str, lease_seconds: int = 30) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE_RUN_STATUSES:
                return False
            if row["execution_owner"] and row["lease_until"] and (
                datetime.fromisoformat(row["lease_until"]) > self._clock()
            ):
                return False
            conn.execute(
                "UPDATE run SET execution_owner=?, lease_until=?, revision=revision+1 "
                "WHERE run_id=? AND revision=?",
                (owner, (self._clock() + timedelta(seconds=lease_seconds)).isoformat(),
                 run_id, row["revision"]),
            )
        return True

    def renew_owner(self, run_id: str, owner: str) -> bool:
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE run SET lease_until=? WHERE run_id=? AND execution_owner=?",
                ((self._clock() + timedelta(seconds=30)).isoformat(), run_id, owner),
            )
            return updated.rowcount == 1

    def release_owner(self, run_id: str, owner: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE run SET execution_owner=NULL, lease_until=NULL "
                "WHERE run_id=? AND execution_owner=?", (run_id, owner),
            )

    def set_invocation_status(
        self, invocation_id: str, status: str, result_json: Any = None
    ) -> None:
        """推进 invocation 状态；result_json 非空时一并落盘（W5/W6 回填）。

        字符串结果若是 JSON 文本（loop 的 tool content 即此形状）先解析成
        对象再落盘——result_json 的口径是结构化结果，不是双重编码。
        """
        if isinstance(result_json, str):
            with contextlib.suppress(TypeError, ValueError):
                result_json = json.loads(result_json)
        payload = None if result_json is None else _json(result_json)
        with self._connect() as conn:
            conn.execute(
                "UPDATE invocation SET status=?, result_json=COALESCE(?, result_json), "
                "updated_at=CURRENT_TIMESTAMP WHERE invocation_id=?",
                (status, payload, invocation_id),
            )

    def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        return self._get_invocation_by("invocation_id", invocation_id)

    def get_invocation_by_token(self, client_token: str) -> dict[str, Any] | None:
        return self._get_invocation_by("client_token", client_token)

    def get_invocation_by_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invocation WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
        return self._invocation_dict(row) if row else None

    def list_invocations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invocation WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [self._invocation_dict(row) for row in rows]

    def get_approval(self, pending_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval WHERE pending_id=?", (pending_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_pending_approvals(self, run_id: str) -> list[dict[str, Any]]:
        """一个 run 的未决审批（含调用身份）：重启后重建待审批卡片的数据源。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.pending_id, a.invocation_id, a.run_id, a.session_id, "
                "a.arguments_fingerprint, a.expected_version, a.expires_at, "
                "a.created_at, i.call_id, i.tool, i.tool_schema_version, "
                "i.arguments_json, i.client_token, i.status AS invocation_status "
                "FROM approval a JOIN invocation i ON i.invocation_id = a.invocation_id "
                "WHERE a.run_id=? AND a.status='pending' ORDER BY a.created_at",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["arguments"] = json.loads(item.pop("arguments_json"))
            result.append(item)
        return result

    @staticmethod
    def _invocation_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["arguments"] = json.loads(result.pop("arguments_json"))
        result["result"] = json.loads(result["result_json"]) if result["result_json"] else None
        return result

    def _get_invocation_by(self, column: str, value: str) -> dict[str, Any] | None:
        assert column in ("invocation_id", "client_token")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM invocation WHERE {column}=?", (value,)
            ).fetchone()
        return self._invocation_dict(row) if row else None


def compute_fingerprint(tool: str, tool_schema_version: str, arguments: dict) -> str:
    """参数指纹（ADR-0008 §2）：绑定工具、签名版本与规范化业务参数。

    落盘与恢复对账共用同一实现——恢复时对 arguments_json 重算并与审批行
    比对，篡改任何一侧都判不匹配。
    """
    arguments_json = _json({k: v for k, v in arguments.items() if k != "client_token"})
    material = f"{tool}{tool_schema_version}{arguments_json}"
    return sha256(material.encode("utf-8")).hexdigest()


def classify_tool_result(content: Any) -> tuple[str, Any]:
    """tool content → (invocation 状态, 结构化结果)，live 与恢复路径共用。

    错误契约 v1（{"error": {...}}）是**确定性业务失败**（MutationError 在
    提交前抛出）→ failed；其余 → succeeded。不确定承诺（超时/执行异常，
    事务可能已提交）不由本函数判定——调用方标 unknown，留给按 token 对账。
    """
    if isinstance(content, str):
        with contextlib.suppress(TypeError, ValueError):
            content = json.loads(content)
    if isinstance(content, dict) and "error" in content:
        return "failed", content
    return "succeeded", content


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
