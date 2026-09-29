"""AgentLoop 单测：单工具回合、max_steps 防护、超时、错误回填（全 mock）。"""

import asyncio
import json

import httpx2
import pytest
from _mock_openai import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.llm import TextDelta, ToolCall, Usage
from agent_core.loop import AgentLoop, LoopConfig, LoopEnd, ToolCallFinished, ToolCallStarted
from agent_core.tools import tool
from pydantic import BaseModel


class OrderQuery(BaseModel):
    order_id: str


class EmptyInput(BaseModel):
    pass


@tool(name="get_order_status", description="查询订单状态", params=OrderQuery)
async def get_order_status(params: OrderQuery) -> dict[str, str]:
    return {"status": "已发货", "order_id": params.order_id}


@tool(name="slow_tool", description="慢工具", params=EmptyInput)
async def slow_tool(params: EmptyInput) -> str:
    await asyncio.sleep(0.5)
    return "done"


@tool(name="boom", description="会抛异常的工具", params=EmptyInput)
async def boom(params: EmptyInput) -> str:
    raise RuntimeError("库存连接断了")


async def _run(agent: AgentLoop, prompt: str = "hi") -> list:
    return [e async for e in agent.run([{"role": "user", "content": prompt}])]


def _handler_pair(requests: list[httpx2.Request], *, first_args: str):
    """两段式端点：首轮返回工具调用，见到 tool 消息后返回最终文本。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        has_tool_result = any(m["role"] == "tool" for m in body["messages"])
        if not has_tool_result:
            return sse_response(tool_call_chunks("call_1", "get_order_status", first_args))
        return sse_response([
            chunk(delta={"content": "订单 123 已发货"}),
            chunk(usage=USAGE),
        ])

    return handler


@pytest.mark.asyncio
async def test_single_tool_task_round_trip() -> None:
    """"查订单 123"单工具任务：请求注入 schema → 执行 → 回填 → 最终回答。"""
    requests: list[httpx2.Request] = []
    agent = AgentLoop(
        make_client(_handler_pair(requests, first_args='{"order_id": "123"}')),
        tools=[get_order_status],
    )
    messages: list = [{"role": "user", "content": "查订单 123 的状态"}]

    events = [e async for e in agent.run(messages)]

    assert events == [
        ToolCallStarted(
            call=ToolCall(id="call_1", name="get_order_status", arguments='{"order_id": "123"}')
        ),
        ToolCallFinished(
            name="get_order_status",
            content=json.dumps({"status": "已发货", "order_id": "123"}, ensure_ascii=False),
            ok=True,
        ),
        TextDelta(text="订单 123 已发货"),
        LoopEnd(
            steps=2,
            usage=Usage(prompt_tokens=26, completion_tokens=14, total_tokens=40),
            completed=True,
        ),
    ]
    # 第一次请求：注入了 tools schema
    assert requests[0]["tools"][0]["function"]["name"] == "get_order_status"
    # 第二次请求：历史里追加了 assistant 工具调用消息与 tool 结果消息
    roles = [m["role"] for m in requests[1]["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assistant = requests[1]["messages"][1]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert requests[1]["messages"][2]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_max_steps_guard_stops_loop() -> None:
    """模型每步都要调工具：到 max_steps 必须停，且 steps 计数正确。"""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return sse_response(tool_call_chunks("call_1", "get_order_status", '{"order_id": "1"}'))

    agent = AgentLoop(
        make_client(handler), tools=[get_order_status], config=LoopConfig(max_steps=2)
    )

    events = await _run(agent)

    assert len(requests) == 2
    assert events[-1] == LoopEnd(
        steps=2,
        usage=Usage(prompt_tokens=26, completion_tokens=14, total_tokens=40),
        completed=False,
    )


@pytest.mark.asyncio
async def test_tool_timeout_returns_error_backfill() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "slow_tool", "{}"))

    agent = AgentLoop(
        make_client(handler), tools=[slow_tool], config=LoopConfig(tool_timeout=0.05)
    )

    events = await _run(agent)

    assert ToolCallFinished(name="slow_tool", content="工具执行超时（>0.05s）", ok=False) in events


@pytest.mark.asyncio
async def test_tool_exception_is_backfilled_not_raised() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "boom", "{}"))

    agent = AgentLoop(make_client(handler), tools=[boom])

    events = await _run(agent)

    assert ToolCallFinished(name="boom", content="工具执行出错：库存连接断了", ok=False) in events


@pytest.mark.asyncio
async def test_unknown_tool_is_backfilled() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "delete_everything", "{}"))

    agent = AgentLoop(make_client(handler))

    events = await _run(agent)

    assert ToolCallFinished(
        name="delete_everything", content="未知工具：delete_everything", ok=False
    ) in events


@pytest.mark.asyncio
async def test_invalid_arguments_are_backfilled() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "get_order_status", '{"nope": 1}'))

    agent = AgentLoop(
        make_client(handler), tools=[get_order_status], config=LoopConfig(max_steps=1)
    )

    events = await _run(agent)

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1
    assert finished[0].ok is False
    assert finished[0].content.startswith("参数校验失败")


def test_duplicate_tool_names_are_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("构造 AgentLoop 时不应发起请求")

    duplicate = tool(name="get_order_status", description="x", params=EmptyInput)(
        get_order_status.handler
    )
    with pytest.raises(ValueError, match="工具重名"):
        AgentLoop(make_client(handler), tools=[get_order_status, duplicate])
