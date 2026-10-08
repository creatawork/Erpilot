"""Erpilot API：LangGraph 会话、checkpoint 和 SSE 流式审批。

路由：
- GET  /healthz           存活探针
- POST /api/chat/stream   SSE 流式对话
- GET  /api/sessions/{id}/state 会话快照
- POST /api/chat/approve/stream 审批后续段
- POST /api/sessions/{id}/resume/stream 从中断节点继续

启动：
    uv run --package erpilot-api python -m erpilot_api
联调前端：cd apps/web && npm install && npm run dev（vite 把 /api 代理到本服务）
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import DEMO_TOOLS
from agent_core.events import LoopEnd
from agent_core.llm import DEFAULT_MODEL, LLMClient, LLMConfig
from agent_core.prices import cost_of
from agent_core.tools import Tool
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from erpilot_api.checkpoint import open_checkpointer
from erpilot_api.events import encode_event, sse_frame
from erpilot_api.recovery import erp_mutation_reconciler
from erpilot_api.run_store import RunStore
from erpilot_api.service import ChatService, SessionError

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


class ResumeRequest(ApproveRequest):
    session_id: str = Field(min_length=1)


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
    checkpointer=None,
    mutation_reconciler=None,
) -> FastAPI:
    """应用工厂：测试注入 mock 的 LLMClient 工厂、临时 trace 目录与工具集。"""
    resolved_tools, env_gate = _resolve_tools(tools)
    if (
        mutation_reconciler is None
        and tools is None
        and os.environ.get("ERPILOT_TOOLS", "").lower() == "mcp"
        and os.environ.get("ERPILOT_WRITES", "").lower() in _WRITES_ENV
    ):
        mutation_reconciler = erp_mutation_reconciler()
    service = ChatService(
        client_factory=client_factory or (lambda: LLMClient(LLMConfig.from_env())),
        model=model,
        trace_dir=trace_dir or DEFAULT_TRACE_DIR,
        tools=resolved_tools,
        approval_gate=approval_gate or env_gate,
        checkpointer=checkpointer,
        run_store=RunStore(
            run_store_path or (trace_dir / "runs.db" if trace_dir else Path("data/runs.db"))
        ),
        mutation_reconciler=mutation_reconciler,
    )

    @asynccontextmanager
    async def lifespan(app):
        if checkpointer is not None:
            yield
        else:
            async with open_checkpointer(
                os.environ.get("ERPILOT_CHECKPOINT_DATABASE_URL", "")
            ) as saver:
                service._checkpointer = saver
                yield
                service._checkpointer = None

    app = FastAPI(title="Erpilot API", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(SessionError)
    async def session_error(request: Request, exc: SessionError):
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": str(exc)}}
        )

    @app.get("/api/sessions/{session_id}/state")
    async def session_state(session_id: str):
        return await service.session_state(session_id)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/runtime")
    def runtime_info() -> dict[str, Any]:
        return {
            "data_source": "custom" if tools is not None else (
                "mcp" if os.environ.get("ERPILOT_TOOLS", "").lower() == "mcp" else "demo"
            ),
            "writes_enabled": any(tool.risk is not None for tool in resolved_tools),
            "reasoning_enabled": os.environ.get("ERPILOT_THINKING", "").lower() in _WRITES_ENV,
        }

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        session_id = req.session_id or uuid4().hex
        return event_response(session_id, service.stream_run(session_id, req.message))

    @app.post("/api/chat/approve/stream")
    async def resume_approval(req: ResumeRequest) -> EventSourceResponse:
        release = await service.reserve_resume(req.session_id, req.pending_id)
        return event_response(
            req.session_id,
            service.stream_resume(
                req.session_id,
                req.pending_id,
                req.approved,
                req.reason,
                reserved=True,
            ),
            release,
        )

    @app.post("/api/sessions/{session_id}/resume/stream")
    async def continue_interrupted_session(session_id: str) -> EventSourceResponse:
        return event_response(session_id, service.stream_retry(session_id))

    def event_response(session_id, stream, release=None):
        heartbeat_interval = float(os.environ.get("ERPILOT_HEARTBEAT_SECONDS", "10") or 10)

        async def generate() -> AsyncIterator[dict[str, str]]:
            yield sse_frame("start", {"session_id": session_id, "model": service.model})
            t0 = time.perf_counter()
            queue: asyncio.Queue[Any] = asyncio.Queue()
            stream_end = object()

            async def produce() -> None:
                # 生产者把流推进到收口，心跳等待不取消底层迭代（设计 5.4）——
                # 反复取消异步迭代会连带取消模型请求或工具调用
                try:
                    async for event in stream:
                        await queue.put(event)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 —— 异常转交消费端统一编码
                    await queue.put(exc)
                finally:
                    await queue.put(stream_end)

            producer = asyncio.create_task(produce())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    except TimeoutError:
                        # 心跳只代表连接存活，不代表模型或工具取得进展；不落展示日志
                        yield sse_frame(
                            "heartbeat", {"at": datetime.now(UTC).isoformat()}
                        )
                        continue
                    if item is stream_end:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    encoded = encode_event(item)
                    if encoded is not None:
                        yield sse_frame(*encoded)
                    if isinstance(item, LoopEnd):
                        yield sse_frame(
                            "done",
                            {
                                "steps": item.steps,
                                "completed": item.completed,
                                "usage": asdict(item.usage) if item.usage else None,
                                "cost": cost_of(service.model, item.usage),
                                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                                "trace": str(service.last_trace) if service.last_trace else None,
                            },
                        )
            except SessionError as exc:
                yield sse_frame("error", {"code": exc.code, "message": str(exc)})
            except Exception as exc:
                yield sse_frame("error", {"code": "execution_error", "message": str(exc)})
            finally:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                await stream.aclose()
                if release:
                    release()

        return EventSourceResponse(
            generate(), background=BackgroundTask(release) if release else None
        )

    @app.post("/api/chat/approve")
    async def approve(req: ApproveRequest) -> dict[str, object]:
        """审批决策回填：挂起中的 SSE 流在决策落定后继续推进。"""
        ok = service.respond_approval(
            req.pending_id, ApprovalDecision(approved=req.approved, reason=req.reason)
        )
        return {"ok": ok}  # False = 未知或已决的 pending_id

    return app


app = create_app()
