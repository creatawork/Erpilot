"""Erpilot API：agent 宿主 + SSE 流式对话（M1 第 4 周最小链路）。

路由：
- GET  /healthz           存活探针
- POST /api/chat/stream   SSE 流式对话（事件协议见 events.py 模块 docstring）

启动：
    uv run --package erpilot-api uvicorn erpilot_api.main:app --reload
联调前端：cd apps/web && npm install && npm run dev（vite 把 /api 代理到本服务）
"""

import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import DEMO_TOOLS
from agent_core.llm import DEFAULT_MODEL, LLMClient, LLMConfig
from agent_core.loop import LoopEnd
from agent_core.prices import cost_of
from agent_core.tools import Tool
from fastapi import FastAPI
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from erpilot_api.events import encode_event, sse_frame
from erpilot_api.run_store import RunStore
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
            return build_agent_tools(writes=True, approval_gate=gate), gate
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
) -> FastAPI:
    """应用工厂：测试注入 mock 的 LLMClient 工厂、临时 trace 目录与工具集。"""
    resolved_tools, env_gate = _resolve_tools(tools)
    service = ChatService(
        client_factory=client_factory or (lambda: LLMClient(LLMConfig.from_env())),
        model=model,
        trace_dir=trace_dir or DEFAULT_TRACE_DIR,
        tools=resolved_tools,
        approval_gate=approval_gate or env_gate,
        # T04 stores read-only runs. Write runs need durable approval/token
        # checkpoints before this store can safely own their lifecycle (T05).
        run_store=(None if any(t.risk for t in resolved_tools) else RunStore(
            run_store_path or (trace_dir / "runs.db" if trace_dir else Path("data/runs.db"))
        )),
    )
    app = FastAPI(title="Erpilot API", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        session_id = req.session_id or uuid4().hex

        async def generate() -> AsyncIterator[dict[str, str]]:
            yield sse_frame("start", {"session_id": session_id, "model": service.model})
            t0 = time.perf_counter()
            stream = service.stream_run(session_id, req.message)
            try:
                async for event in stream:
                    encoded = encode_event(event)
                    if encoded is not None:
                        yield sse_frame(*encoded)
                    if isinstance(event, LoopEnd):
                        yield sse_frame(
                            "done",
                            {
                                "steps": event.steps,
                                "completed": event.completed,
                                "usage": asdict(event.usage) if event.usage else None,
                                "cost": cost_of(service.model, event.usage),
                                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                                "trace": str(service.last_trace) if service.last_trace else None,
                            },
                        )
            except Exception as exc:  # LLM 网络错误 / 缺 API key 等：流内报错后收口
                yield sse_frame("error", {"message": str(exc)})
            finally:
                await stream.aclose()

        return EventSourceResponse(generate())

    @app.post("/api/chat/approve")
    async def approve(req: ApproveRequest) -> dict[str, object]:
        """审批决策回填：挂起中的 SSE 流在决策落定后继续推进。"""
        ok = service.respond_approval(
            req.pending_id, ApprovalDecision(approved=req.approved, reason=req.reason)
        )
        return {"ok": ok}  # False = 未知或已决的 pending_id

    return app


app = create_app()
