"""Erpilot API：agent 宿主 + SSE 流式对话（M1 第 4 周最小链路）。

路由：
- GET  /healthz           存活探针
- POST /api/chat/stream   SSE 流式对话（事件协议见 events.py 模块 docstring）

启动：
    uv run --package erpilot-api uvicorn erpilot_api.main:app --reload
联调前端：cd apps/web && npm install && npm run dev（vite 把 /api 代理到本服务）
"""

import os
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import DEMO_TOOLS
from agent_core.llm import DEFAULT_MODEL, LLMClient, LLMConfig
from agent_core.tools import Tool
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from erpilot_api.events import sse_frame
from erpilot_api.execution import RunManager
from erpilot_api.recovery import RecoveryError, build_erp_recovery
from erpilot_api.run_store import ActiveRunError, ApprovalStateError, RunStore, SchemaVersionError
from erpilot_api.service import ChatService

DEFAULT_TRACE_DIR = Path("traces")

_WRITES_ENV = ("1", "true", "yes")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, description="用户本轮输入")
    session_id: str | None = Field(default=None, description="缺省则新建会话")


class ApproveRequest(BaseModel):
    """审批决策回填（M4 第 2 周）：pending_id 来自 approval_pending 事件。"""

    pending_id: str = Field(min_length=1, description="待审批请求 id")
    approved: bool = Field(description="true=批准执行，false=拒绝")
    reason: str = Field(default="", description="拒绝原因（转述给模型与用户）")
    run_id: str | None = None
    session_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    arguments_fingerprint: str | None = None


def _resolve_tools(
    tools: Sequence[Tool] | None,
) -> tuple[Sequence[Tool], StreamApprovalGate | None]:
    """工具来源：显式参数 > ERPILOT_TOOLS=mcp（经 MCP 桥查真数据）> demo 假数据。

    mcp 解析在 create_app 时同步完成（此时无运行中的事件循环），失败即抛——
    显式选了真数据而库不存在，带着指引报错好过静默降级。ERPILOT_WRITES=1 时
    写工具进入工具面并挂挂起式审批门（决策经 POST /api/chat/approve 回填）。
    """
    if tools is not None:
        return tools, None
    if os.environ.get("ERPILOT_TOOLS", "").lower() == "mcp":
        from mcp_erp import build_agent_tools

        if os.environ.get("ERPILOT_WRITES", "").lower() in _WRITES_ENV:
            gate = StreamApprovalGate()
            return (
                build_agent_tools(
                    writes=True,
                    approval_gate=gate,
                    token_factory=lambda: uuid4().hex,  # T05：审批展示前生成稳定幂等键
                ),
                gate,
            )
        return build_agent_tools(), None
    return list(DEMO_TOOLS), None


def create_app(
    *,
    client_factory: Callable[[], LLMClient] | None = None,
    model: str = DEFAULT_MODEL,
    trace_dir: Path | None = None,
    tools: Sequence[Tool] | None = None,
    approval_gate: StreamApprovalGate | None = None,
    run_store_path: Path | None = None,
    erp_db_path: Path | None = None,
) -> FastAPI:
    """应用工厂：测试注入 mock 的 LLMClient 工厂、临时 trace 目录与工具集。"""
    resolved_tools, env_gate = _resolve_tools(tools)
    store = RunStore(
        run_store_path or (trace_dir / "runs.db" if trace_dir else Path("data/runs.db"))
    )
    service = ChatService(
        client_factory=client_factory or (lambda: LLMClient(LLMConfig.from_env())),
        model=model,
        trace_dir=trace_dir or DEFAULT_TRACE_DIR,
        tools=resolved_tools,
        approval_gate=approval_gate or env_gate,
        # T05 起写运行也持久化：写意图（token/call_id/参数/审批）在展示前落盘，
        # 提交结果按 token 对账（T06），运行存储可安全承载写 run 生命周期。
        run_store=store,
    )
    if erp_db_path is None and os.environ.get("ERPILOT_TOOLS", "").lower() == "mcp":
        from erp_store.db import DEFAULT_DB
        erp_db_path = DEFAULT_DB
    recovery = (
        build_erp_recovery(store, str(erp_db_path), list(resolved_tools)) if erp_db_path else None
    )
    runs = RunManager(service, store, recovery)
    @asynccontextmanager
    async def lifespan(app):
        yield
        await runs.close()

    app = FastAPI(title="Erpilot API", version="0.1.0", lifespan=lifespan)
    app.state.runs = runs

    def snapshot(run_id):
        try:
            store.get_run(run_id)  # 拒绝不支持的检查点版本，保留源数据。
            return store.snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except SchemaVersionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        session_id = req.session_id or uuid4().hex
        try:
            run_id = runs.start(session_id, req.message)
        except ActiveRunError as exc:
            raise HTTPException(409, str(exc)) from exc
        async def generate():
            async for event in runs.events(run_id):
                yield sse_frame(event["event"], event["data"])
        return EventSourceResponse(generate())

    @app.post("/api/runs")
    async def start_run(req: ChatRequest):
        try:
            run_id = runs.start(req.session_id or uuid4().hex, req.message)
        except ActiveRunError as exc:
            raise HTTPException(409, str(exc)) from exc
        return snapshot(run_id)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return snapshot(run_id)

    @app.get("/api/sessions/{session_id}/runs")
    async def session_runs(session_id: str):
        if store.get_session(session_id) is None:
            raise HTTPException(404, "会话不存在")
        return {"session_id": session_id, "runs": store.list_runs(session_id)}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, after_seq: int = Query(default=0, ge=0)):
        snapshot(run_id)
        async def generate():
            async for event in runs.events(run_id, after_seq):
                yield sse_frame(event["event"], event["data"])
        return EventSourceResponse(generate())

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str):
        snapshot(run_id)
        try:
            return await runs.resume(run_id)
        except RecoveryError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        snapshot(run_id)
        return await runs.cancel(run_id)

    @app.post("/api/chat/approve")
    async def approve(req: ApproveRequest) -> dict[str, object]:
        """审批决策回填：挂起中的 SSE 流在决策落定后继续推进。"""
        if req.run_id is not None and store.get_approval(req.pending_id) is None:
            raise HTTPException(404, "审批不存在")
        try:
            ok = service.respond_approval(
                req.pending_id, ApprovalDecision(approved=req.approved, reason=req.reason),
                run_id=req.run_id, session_id=req.session_id, expected_version=req.expected_version,
                arguments_fingerprint=req.arguments_fingerprint,
            )
        except ApprovalStateError as exc:
            raise HTTPException(exc.status_code, {"message": str(exc),
                                                 "approval": exc.approval}) from exc
        return {"ok": ok}

    return app


app = create_app()
