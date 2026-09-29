"""LLM client 单测：httpx2.MockTransport 伪 OpenAI 兼容端点的 SSE 流（不烧真实 token）。

注：计划里的 respx 针对 httpx；openai SDK 3.x 底层已换 httpx2，respx 拦截不到，
故改用 httpx2.AsyncClient(transport=MockTransport) 注入 LLMClient——同样是传输层
mock，且 SDK 的 SSE 解析也留在测试路径内。
"""

import json

import httpx2
import pytest
from agent_core.llm import LLMClient, LLMConfig, StreamEnd, StreamResult, TextDelta, Usage
from openai import AsyncOpenAI

BASE_URL = "https://llm.test/api/v1/"
SSE_HEADERS = {"content-type": "text/event-stream"}
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


def _sse_response(chunks: list[dict]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    body += "data: [DONE]\n\n"
    return httpx2.Response(200, content=body.encode(), headers=SSE_HEADERS)


def _chunk(*, delta: dict | None = None, usage: dict | None = None) -> dict:
    chunk: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "glm-5.3-flash",
        "choices": [],
    }
    if delta is not None:
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _client(handler) -> LLMClient:
    """LLMClient + 注入 MockTransport 的 AsyncOpenAI，请求落到 handler。"""
    config = LLMConfig(api_key="test-key", base_url=BASE_URL, model="glm-5.3-flash")
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    oai = AsyncOpenAI(api_key="test-key", base_url=BASE_URL, http_client=http)
    return LLMClient(config, client=oai)


# 典型流：role 首包（content 为空）→ 两段正文 → 末包带 usage、choices 为空
STREAM_CHUNKS = [
    _chunk(delta={"role": "assistant", "content": ""}),
    _chunk(delta={"content": "你好"}),
    _chunk(delta={"content": "，世界"}),
    _chunk(usage=USAGE),
]


async def _collect(client: LLMClient) -> list:
    return [e async for e in client.stream_chat([{"role": "user", "content": "hi"}])]


@pytest.mark.asyncio
async def test_stream_yields_deltas_then_single_end() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _sse_response(STREAM_CHUNKS)

    events = await _collect(_client(handler))

    assert events == [
        TextDelta(text="你好"),
        TextDelta(text="，世界"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_chat_returns_full_text_and_usage() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response(STREAM_CHUNKS)

    result = await _client(handler).chat([{"role": "user", "content": "hi"}])

    assert result == StreamResult(
        text="你好，世界", usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)
    )


@pytest.mark.asyncio
async def test_request_uses_configured_model_and_include_usage() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _sse_response(STREAM_CHUNKS)

    await _client(handler).chat([{"role": "user", "content": "hi"}])

    body = json.loads(requests[0].content)
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_stream_without_usage_still_ends_once() -> None:
    """端点不回 usage 时，也要保证恰好一个 StreamEnd(None) 收尾。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response([_chunk(delta={"content": "ok"})])

    events = await _collect(_client(handler))

    assert events == [TextDelta(text="ok"), StreamEnd(usage=None)]


@pytest.mark.asyncio
async def test_stream_stops_right_after_usage_chunk() -> None:
    """usage 包即收口：其后即使端点还发内容包也不产出（守住"恰好一个 StreamEnd"）。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        chunks = [
            _chunk(delta={"content": "前"}),
            _chunk(usage=USAGE),
            _chunk(delta={"content": "后"}),
        ]
        return _sse_response(chunks)

    events = await _collect(_client(handler))

    assert events == [
        TextDelta(text="前"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]
