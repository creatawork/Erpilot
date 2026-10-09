"""LLM client 单测：流式事件、工具调用归并、结构化输出（全 mock，不烧真实 token）。"""

import json

import httpx2
import pytest
from agent_core.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMConfig,
    ReasoningDelta,
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


# ---- 阶段二：思考字段（reasoning_content）----


def _reasoning_chunks() -> list[dict]:
    return [
        chunk(delta={"role": "assistant", "content": "", "reasoning_content": "先算 9.9×8"}),
        chunk(delta={"content": "", "reasoning_content": "≈79.2"}),
        chunk(delta={"content": "79.2"}),
        chunk(usage=USAGE),
    ]


@pytest.mark.asyncio
async def test_thinking_enabled_yields_reasoning_before_text() -> None:
    """思考独立成事件：不拼入 TextDelta，先于正文出现。"""
    client = make_client(lambda request: sse_response(_reasoning_chunks()))
    import dataclasses

    client._config = dataclasses.replace(client.config, thinking=True)
    events = await _collect(client, [{"role": "user", "content": "9.9×8"}])
    assert events == [
        ReasoningDelta(text="先算 9.9×8"),
        ReasoningDelta(text="≈79.2"),
        TextDelta(text="79.2"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]


@pytest.mark.asyncio
async def test_thinking_disabled_drops_reasoning_silently() -> None:
    """默认关闭：reasoning_content 被丢弃，正文流不受影响。"""
    client = make_client(lambda request: sse_response(_reasoning_chunks()))
    events = await _collect(client, [{"role": "user", "content": "9.9×8"}])
    assert events == [
        TextDelta(text="79.2"),
        StreamEnd(usage=Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)),
    ]


@pytest.mark.asyncio
async def test_chat_excludes_reasoning_from_final_text() -> None:
    import dataclasses

    client = make_client(lambda request: sse_response(_reasoning_chunks()))
    client._config = dataclasses.replace(client.config, thinking=True)
    result = await client.chat([{"role": "user", "content": "9.9×8"}])
    assert result.text == "79.2"


# ---- 批次 1：多模型 provider profile（from_env，纯 env mock，不烧 token）----


def _clear_llm_env(monkeypatch) -> None:
    for name in (
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "ERPILOT_THINKING",
        "ZHIPU_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_from_env_defaults_to_zhipu(monkeypatch) -> None:
    """不设 LLM_PROVIDER 时与旧版一致：读 ZHIPU_API_KEY + GLM 默认端点/模型。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    config = LLMConfig.from_env()
    assert (config.api_key, config.base_url, config.model) == (
        "zk",
        DEFAULT_BASE_URL,
        DEFAULT_MODEL,
    )


def test_llm_config_repr_hides_api_key() -> None:
    config = LLMConfig(api_key="private-test-key")

    assert "private-test-key" not in repr(config)


def test_from_env_deepseek_profile(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    config = LLMConfig.from_env()
    assert config.api_key == "dk"
    assert config.base_url == "https://api.deepseek.com/v1"
    assert config.model == "deepseek-chat"


def test_from_env_qwen_profile(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qk")
    config = LLMConfig.from_env()
    assert config.api_key == "qk"
    assert config.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.model == "qwen-plus"


def test_from_env_provider_is_case_insensitive(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "DeepSeek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    assert LLMConfig.from_env().model == "deepseek-chat"


def test_from_env_base_url_and_model_override_profile(monkeypatch) -> None:
    """LLM_BASE_URL / LLM_MODEL 覆盖供应商预设（兼容旧的中转端点用法）。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-reasoner")
    config = LLMConfig.from_env()
    assert config.base_url == "https://proxy.example/v1"
    assert config.model == "deepseek-reasoner"


def test_from_env_unknown_provider_raises(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    with pytest.raises(RuntimeError, match="未知 LLM_PROVIDER"):
        LLMConfig.from_env()


def test_from_env_missing_key_names_provider_env(monkeypatch) -> None:
    """key 缺失的报错须点名该供应商对应的环境变量。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        LLMConfig.from_env()
