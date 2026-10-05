"""测试共享件：用 httpx2.MockTransport 伪造 OpenAI 兼容端点的 SSE 流（不烧真实 token）。

M1 第 4 周从 agent_core/tests/_mock_openai.py 提升为包内模块——apps/api 的
链路测试要在同一个 HTTP 边界上 mock，两处共用同一套 chunk 构造。
本模块只在测试里导入：agent-core 的运行时依赖仍是 openai + pydantic（httpx2
由工作区 dev 依赖组提供）。
"""

import json
from collections.abc import Callable

import httpx2
from openai import AsyncOpenAI

from agent_core.llm import LLMClient, LLMConfig

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
    return multi_tool_chunks([(call_id, name, arguments)], usage=usage)


def multi_tool_chunks(
    calls: list[tuple[str, str, str]], *, usage: dict | None = USAGE
) -> list[dict]:
    """一轮多个工具调用：首包同时给出全部 id/name，随后各 arguments 一包，末包 usage。"""
    chunks = [
        chunk(delta={"tool_calls": [
            {"index": i, "id": cid, "type": "function",
             "function": {"name": name, "arguments": ""}}
            for i, (cid, name, _) in enumerate(calls)
        ]})
    ]
    for i, (_, _, arguments) in enumerate(calls):
        mid = max(1, len(arguments) // 2)  # arguments 拆两段，覆盖流式增量归并
        first = {"index": i, "function": {"arguments": arguments[:mid]}}
        second = {"index": i, "function": {"arguments": arguments[mid:]}}
        chunks.append(chunk(delta={"tool_calls": [first]}))
        chunks.append(chunk(delta={"tool_calls": [second]}))
    chunks.append(chunk(usage=usage))
    return chunks


def make_client(
    handler: Callable[[httpx2.Request], httpx2.Response], *, max_retries: int = 2
) -> LLMClient:
    """LLMClient + 注入 MockTransport 的 AsyncOpenAI，请求落到 handler。

    max_retries 是 openai SDK 自身的传输层重试（默认 2）；要测调用方自己的
    重试策略时传 0，让错误直达上层。
    """
    config = LLMConfig(api_key="test-key", base_url=BASE_URL, model="glm-5.3-flash")
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    oai = AsyncOpenAI(
        api_key="test-key", base_url=BASE_URL, http_client=http, max_retries=max_retries
    )
    return LLMClient(config, client=oai)
