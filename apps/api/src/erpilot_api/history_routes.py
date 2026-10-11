"""Read-only local session discovery; checkpoints remain execution authority."""

from typing import Literal

from fastapi import FastAPI, Query
from pydantic import BaseModel

from erpilot_api.service import SessionError


class SessionSummary(BaseModel):
    session_id: str
    title: str
    updated_at: str
    status: Literal[
        "running",
        "waiting_approval",
        "recovering",
        "reconciliation_required",
        "unknown",
        "completed",
        "failed",
        "cancelled",
    ]


class SessionPage(BaseModel):
    items: list[SessionSummary]
    has_more: bool


def register_history_routes(app: FastAPI, store):
    @app.get("/api/sessions", response_model=SessionPage)
    def list_sessions(
        limit: int = Query(default=20, ge=1, le=50), offset: int = Query(default=0, ge=0)
    ):
        if store is None:
            raise SessionError(
                503,
                "history_disabled",
                "当前运行环境未启用历史会话",
                "请使用配置了会话存储的运行环境",
            )
        return store.list_sessions(limit=limit, offset=offset)
