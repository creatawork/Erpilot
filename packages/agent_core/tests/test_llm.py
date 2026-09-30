"""LLM client 单测：流式事件、工具调用归并、结构化输出（全 mock，不烧真实 token）。"""

import json

import httpx2
import pytest
from agent_core.llm import (
    StreamEnd,
    StreamResult,
    StructuredResult,
    TextDelta,
    ToolCall,
    Usage,
)
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from pydantic import BaseModel, ValidationError


async def _collect(client, *args, **kwargs) -> list:
    return [e async for e in client.stream_chat(*args, **kwargs)]


# ---- 第 1 周：流式 + usage ----


@pytest.mark.asyncio
async def test_stream_yields_deltas_then_single_end() -> None:
    # 典型流：role 首包（content 为空）→ 两段正文 → 末包带 usage、choices 为空
    chunks = [
        chunk(delta={"role": "assistant", "content": ""}),
        chunk(delta={"content": "你好"}),
        chunk(delta={"content": "，世界"}),
        chunk(usage=USAGE),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(chunks)

    events = await _collect(make_client(handler), [{"role": "user", "content": "hi"}])

    assert events == [
        TextDelta(text="你好"),
        TextDelta(text="，世界"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]


@pytest.mark.asyncio
async def test_chat_returns_full_text_and_usage() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response([
            chunk(delta={"content": "你好"}),
            chunk(delta={"content": "，世界"}),
            chunk(usage=USAGE),
        ])

    result = await make_client(handler).chat([{"role": "user", "content": "hi"}])

    assert result == StreamResult(
        text="你好，世界", usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)
    )


@pytest.mark.asyncio
async def test_request_uses_configured_model_and_include_usage() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return sse_response([chunk(delta={"content": "ok"}), chunk(usage=USAGE)])

    await make_client(handler).chat([{"role": "user", "content": "hi"}])

    body = json.loads(requests[0].content)
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in body  # 未传工具时不注入 tools 字段


@pytest.mark.asyncio
async def test_stream_without_usage_still_ends_once() -> None:
    """端点不回 usage 时，也要保证恰好一个 StreamEnd(None) 收尾。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response([chunk(delta={"content": "ok"})])

    events = await _collect(make_client(handler), [{"role": "user", "content": "hi"}])

    assert events == [TextDelta(text="ok"), StreamEnd(usage=None)]


@pytest.mark.asyncio
async def test_stream_stops_right_after_usage_chunk() -> None:
    """usage 包即收口：其后即使端点还发内容包也不产出（守住"恰好一个 StreamEnd"）。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        chunks = [
            chunk(delta={"content": "前"}),
            chunk(usage=USAGE),
            chunk(delta={"content": "后"}),
        ]
        return sse_response(chunks)

    events = await _collect(make_client(handler), [{"role": "user", "content": "hi"}])

    assert events == [
        TextDelta(text="前"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]


# ---- 第 2 周：工具调用归并 ----


@pytest.mark.asyncio
async def test_stream_merges_tool_call_deltas() -> None:
    """分段的 arguments 增量按 index 归并成一个 ToolCall 事件，且先于 StreamEnd。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response([
            chunk(delta={"content": "我查一下"}),
            *tool_call_chunks("call_1", "get_order_status", '{"order_id": "123"}'),
        ])

    events = await _collect(make_client(handler), [{"role": "user", "content": "hi"}])

    assert events == [
        TextDelta(text="我查一下"),
        ToolCall(id="call_1", name="get_order_status", arguments='{"order_id": "123"}'),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]


@pytest.mark.asyncio
async def test_chat_collects_tool_calls() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "get_order_status", "{}"))

    result = await make_client(handler).chat([{"role": "user", "content": "hi"}])

    assert result.tool_calls == [ToolCall(id="call_1", name="get_order_status", arguments="{}")]


@pytest.mark.asyncio
async def test_request_injects_openai_tool_schemas() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return sse_response(tool_call_chunks("call_1", "get_order_status", "{}"))

    tools = [{"type": "function", "function": {"name": "get_order_status"}}]
    events = await _collect(
        make_client(handler), [{"role": "user", "content": "hi"}], tools=tools
    )
    assert events  # 消费事件流确保请求真实发生

    body = json.loads(requests[0].content)
    assert body["tools"] == tools


# ---- 第 2 周：结构化输出 ----


class Person(BaseModel):
    name: str
    age: int


@pytest.mark.asyncio
async def test_structured_parses_json_and_appends_instruction() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return sse_response([chunk(delta={"content": '{"name": "李四", "age": 30}'})])

    result = await make_client(handler).structured([{"role": "user", "content": "hi"}], Person)

    assert result == StructuredResult(
        data=Person(name="李四", age=30),
        usage=None,
    )
    last_message = json.loads(requests[0].content)["messages"][-1]
    assert last_message["role"] == "system"
    assert "JSON Schema" in last_message["content"]


@pytest.mark.asyncio
async def test_structured_strips_markdown_fence() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response([chunk(delta={"content": '```json\n{"name": "张三", "age": 20}\n```'})])

    result = await make_client(handler).structured([{"role": "user", "content": "hi"}], Person)

    assert result.data == Person(name="张三", age=20)


@pytest.mark.asyncio
async def test_structured_invalid_output_raises() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response([chunk(delta={"content": "这不是 JSON"})])

    with pytest.raises(ValidationError):
        await make_client(handler).structured([{"role": "user", "content": "hi"}], Person)
