"""测试共享件：用 httpx2.MockTransport 伪造 OpenAI 兼容端点的 SSE 流（不烧真实 token）。"""

import json
from collections.abc import Callable

import httpx2
from agent_core.llm import LLMClient, LLMConfig
from openai import AsyncOpenAI

BASE_URL = "https://llm.test/api/v1/"
SSE_HEADERS = {"content-type": "text/event-stream"}
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


def sse_response(chunks: list[dict]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    body += "data: [DONE]\n\n"
    return httpx2.Response(200, content=body.encode(), headers=SSE_HEADERS)


def chunk(*, delta: dict | None = None, usage: dict | None = None) -> dict:
    out: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "glm-5.3-flash",
        "choices": [],
    }
    if delta is not None:
        out["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
    if usage is not None:
        out["usage"] = usage
    return out


def tool_call_chunks(
    call_id: str, name: str, arguments: str, *, usage: dict | None = USAGE
) -> list[dict]:
    """把一次工具调用拆成典型流：id/name 首包 → arguments 分两段 → 末包 usage。"""
    return [
        chunk(delta={"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": ""}},
        ]}),
        chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": arguments[:2]}}]}),
        chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": arguments[2:]}}]}),
        chunk(usage=usage),
    ]


def make_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> LLMClient:
    """LLMClient + 注入 MockTransport 的 AsyncOpenAI，请求落到 handler。"""
    config = LLMConfig(api_key="test-key", base_url=BASE_URL, model="glm-5.3-flash")
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    oai = AsyncOpenAI(api_key="test-key", base_url=BASE_URL, http_client=http)
    return LLMClient(config, client=oai)
