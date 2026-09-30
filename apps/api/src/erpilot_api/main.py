"""Erpilot API：agent 宿主 + SSE 流式对话（M1 第 4 周最小链路）。

路由：
- GET  /healthz           存活探针
- POST /api/chat/stream   SSE 流式对话（事件协议见 events.py 模块 docstring）

启动：
    uv run --package erpilot-api uvicorn erpilot_api.main:app --reload
联调前端：cd apps/web && npm install && npm run dev（vite 把 /api 代理到本服务）
"""

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from agent_core.llm import DEFAULT_MODEL, LLMClient, LLMConfig
from agent_core.loop import LoopEnd
from agent_core.prices import cost_of
from fastapi import FastAPI
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from erpilot_api.events import encode_event, sse_frame
from erpilot_api.service import ChatService

DEFAULT_TRACE_DIR = Path("traces")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, description="用户本轮输入")
    session_id: str | None = Field(default=None, description="缺省则新建会话")


def create_app(
    *,
    client_factory: Callable[[], LLMClient] | None = None,
    model: str = DEFAULT_MODEL,
    trace_dir: Path | None = None,
) -> FastAPI:
    """应用工厂：测试注入 mock 的 LLMClient 工厂与临时 trace 目录。"""
    service = ChatService(
        client_factory=client_factory or (lambda: LLMClient(LLMConfig.from_env())),
        model=model,
        trace_dir=trace_dir or DEFAULT_TRACE_DIR,
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
            try:
                async for event in service.stream_run(session_id, req.message):
                    encoded = encode_event(event)
                    if encoded is not None:
                        yield sse_frame(*encoded)
                    if isinstance(event, LoopEnd):
                        yield sse_frame("done", {
                            "steps": event.steps,
                            "completed": event.completed,
                            "usage": asdict(event.usage) if event.usage else None,
                            "cost": cost_of(service.model, event.usage),
                            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                            "trace": str(service.last_trace) if service.last_trace else None,
                        })
            except Exception as exc:  # LLM 网络错误 / 缺 API key 等：流内报错后收口
                yield sse_frame("error", {"message": str(exc)})

        return EventSourceResponse(generate())

    return app


app = create_app()
