"""AgentLoop 单测：单/多工具回合、max_steps 防护、超时、错误回填、并行、上下文压缩。"""

import asyncio
import json

import httpx2
import pytest
from agent_core.context import DROPPED_NOTE, ContextPolicy
from agent_core.llm import TextDelta, ToolCall, Usage
from agent_core.loop import (
    AgentLoop,
    LoopConfig,
    LoopEnd,
    StepEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    ToolRetryPolicy,
)
from agent_core.testing import (
    USAGE,
    chunk,
    make_client,
    multi_tool_chunks,
    sse_response,
    tool_call_chunks,
)
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


@tool(name="slow_report", description="慢工具（0.2s）", params=EmptyInput)
async def slow_report(params: EmptyInput) -> str:
    await asyncio.sleep(0.2)
    return "慢结果"


@tool(name="fast_report", description="快工具（0.02s）", params=EmptyInput)
async def fast_report(params: EmptyInput) -> str:
    await asyncio.sleep(0.02)
    return "快结果"


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
        return sse_response(
            [
                chunk(delta={"content": "订单 123 已发货"}),
                chunk(usage=USAGE),
            ]
        )

    return handler


@pytest.mark.asyncio
async def test_single_tool_task_round_trip() -> None:
    """ "查订单 123"单工具任务：请求注入 schema → 执行 → 回填 → 最终回答。"""
    requests: list[httpx2.Request] = []
    agent = AgentLoop(
        make_client(_handler_pair(requests, first_args='{"order_id": "123"}')),
        tools=[get_order_status],
    )
    messages: list = [{"role": "user", "content": "查订单 123 的状态"}]

    events = [e async for e in agent.run(messages)]

    # 轮次边界事件（StepStarted/StepEnd 含耗时，不作精确相等断言），其余逐个比对
    core = [e for e in events if not isinstance(e, (StepStarted, StepEnd))]
    assert core == [
        ToolCallStarted(
            call=ToolCall(id="call_1", name="get_order_status", arguments='{"order_id": "123"}')
        ),
        ToolCallFinished(
            call_id="call_1",
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
    assert [e.step for e in events if isinstance(e, StepStarted)] == [1, 2]
    step_ends = [e for e in events if isinstance(e, StepEnd)]
    assert [e.step for e in step_ends] == [1, 2]
    assert all(e.usage == Usage(13, 7, 20) and e.duration_ms >= 0 for e in step_ends)
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
        make_client(handler),
        tools=[slow_tool],
        config=LoopConfig(max_steps=1, tool_timeout=0.05, retry=ToolRetryPolicy(retries=0)),
    )

    events = await _run(agent)

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1 and finished[0].ok is False
    assert json.loads(finished[0].content)["error"]["type"] == "timeout"


@pytest.mark.asyncio
async def test_tool_exception_is_backfilled_not_raised() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "boom", "{}"))

    agent = AgentLoop(
        make_client(handler),
        tools=[boom],
        config=LoopConfig(max_steps=1, retry=ToolRetryPolicy(retries=0)),
    )

    events = await _run(agent)

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1 and finished[0].ok is False
    payload = json.loads(finished[0].content)
    assert payload["error"]["type"] == "execution"
    assert "库存连接断了" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_unknown_tool_is_backfilled() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "delete_everything", "{}"))

    agent = AgentLoop(make_client(handler), config=LoopConfig(max_steps=1))

    events = await _run(agent)

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1 and finished[0].ok is False
    payload = json.loads(finished[0].content)
    assert payload["error"]["type"] == "unknown_tool"
    assert "delete_everything" in payload["error"]["message"]


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
    payload = json.loads(finished[0].content)
    assert payload["error"]["type"] == "validation"


def test_duplicate_tool_names_are_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("构造 AgentLoop 时不应发起请求")

    duplicate = tool(name="get_order_status", description="x", params=EmptyInput)(
        get_order_status.handler
    )
    with pytest.raises(ValueError, match="工具重名"):
        AgentLoop(make_client(handler), tools=[get_order_status, duplicate])


# ---- 第 3 周：重试策略、并行调用、上下文压缩 ----


@pytest.mark.asyncio
async def test_transient_error_is_retried_until_success() -> None:
    calls = {"n": 0}

    @tool(name="flaky", description="首次超时，重试成功", params=EmptyInput)
    async def flaky(params: EmptyInput) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.2)  # 超过 0.05s 超时线
        return "recovered"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return sse_response(tool_call_chunks("call_1", "flaky", "{}"))

    agent = AgentLoop(
        make_client(handler),
        tools=[flaky],
        config=LoopConfig(
            max_steps=1, tool_timeout=0.05, retry=ToolRetryPolicy(retries=1, backoff=0)
        ),
    )

    events = await _run(agent)

    assert calls["n"] == 2  # 确实重试了一次
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1  # 重试发生在执行层，对外只暴露最终结果
    assert finished[0].ok is True and finished[0].content == "recovered"


@pytest.mark.asyncio
async def test_parallel_tool_calls_complete_out_of_order() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "汇总完成"}), chunk(usage=USAGE)])
        return sse_response(
            multi_tool_chunks(
                [
                    ("call_1", "slow_report", "{}"),
                    ("call_2", "fast_report", "{}"),
                ]
            )
        )

    agent = AgentLoop(make_client(handler), tools=[slow_report, fast_report])

    events = await _run(agent)

    started = [e for e in events if isinstance(e, ToolCallStarted)]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call.name for e in started] == ["slow_report", "fast_report"]
    assert [e.name for e in finished] == ["fast_report", "slow_report"]  # 并发：快的先完成
    # 结果消息仍按调用顺序回填历史
    tool_ids = [m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"]
    assert tool_ids == ["call_1", "call_2"]
    assert events[-1] == LoopEnd(
        steps=2,
        usage=Usage(prompt_tokens=26, completion_tokens=14, total_tokens=40),
        completed=True,
    )


@pytest.mark.asyncio
async def test_context_compression_applied_in_place() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "好的"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks("call_1", "get_order_status", '{"order_id": "1"}'))

    agent = AgentLoop(
        make_client(handler),
        tools=[get_order_status],
        config=LoopConfig(max_steps=2, context=ContextPolicy(max_tokens=30, keep_last_messages=2)),
    )
    messages: list = [{"role": "user", "content": "问" * 300}]  # ≈101 tokens，超 30 预算

    events = [e async for e in agent.run(messages)]

    # 超预算的最老轮次被整轮裁掉（提问被省略），工具交换成对保留，提示已插入
    assert [m["role"] for m in messages] == ["system", "assistant", "tool", "assistant"]
    assert any(m.get("content") == DROPPED_NOTE for m in messages)
    assert events[-1].completed is True and events[-1].steps == 2


# ---- 挂起式审批流（M4 第 2 周，ADR-0006） ----

from agent_core.approval import (  # noqa: E402
    RISK_SINGLE_CONFIRM,
    ApprovalDecision,
    StreamApprovalGate,
    guarded,
)
from agent_core.loop import ApprovalPending, ApprovalResolved  # noqa: E402


class WriteInput(BaseModel):
    sku: str


@tool(
    name="create_order_t",
    description="建单（需审批）",
    params=WriteInput,
    risk=RISK_SINGLE_CONFIRM,
)
async def create_order_t(params: WriteInput) -> dict[str, str]:
    return {"created": params.sku}


async def _run_with_approval(
    agent: AgentLoop,
    gate: StreamApprovalGate,
    decision: ApprovalDecision,
    prompt: str = "hi",
    respond_after: float = 0.0,
) -> list:
    events: list = []

    async def respond_later(pending_id: str) -> None:
        if respond_after:
            await asyncio.sleep(respond_after)
        assert gate.respond(pending_id, decision)

    async for event in agent.run([{"role": "user", "content": prompt}]):
        events.append(event)
        if isinstance(event, ApprovalPending):
            asyncio.create_task(respond_later(event.pending_id))
    return events


def _write_handler_pair(requests: list[httpx2.Request], *, call_id: str = "call_w"):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "处理完成"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks(call_id, "create_order_t", '{"sku": "A1001"}'))

    return handler


@pytest.mark.asyncio
async def test_suspended_approval_approved_executes_and_backfills() -> None:
    requests: list[httpx2.Request] = []
    gate = StreamApprovalGate()
    agent = AgentLoop(
        make_client(_write_handler_pair(requests)), tools=[guarded(create_order_t, gate)]
    )

    messages: list = [{"role": "user", "content": "下单"}]
    events = []
    async for event in agent.run(messages):
        events.append(event)
        if isinstance(event, ApprovalPending):
            assert event.tool == "create_order_t"
            assert event.risk == RISK_SINGLE_CONFIRM
            assert event.arguments == {"sku": "A1001"}
            assert gate.respond(event.pending_id, ApprovalDecision(approved=True))

    names = [type(e).__name__ for e in events]
    assert names == [
        "StepStarted",
        "StepEnd",
        "ToolCallStarted",
        "ApprovalPending",
        "ApprovalResolved",
        "ToolCallFinished",
        "StepStarted",
        "TextDelta",
        "StepEnd",
        "LoopEnd",
    ]
    resolved = next(e for e in events if isinstance(e, ApprovalResolved))
    assert resolved.approved is True
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.ok and json.loads(finished.content) == {"created": "A1001"}
    # 决策后的真实结果按协议回填进消息历史
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert json.loads(tool_msgs[0]["content"]) == {"created": "A1001"}


@pytest.mark.asyncio
async def test_suspended_approval_denied_backfills_not_executed() -> None:
    requests: list[httpx2.Request] = []
    gate = StreamApprovalGate()
    agent = AgentLoop(
        make_client(_write_handler_pair(requests)), tools=[guarded(create_order_t, gate)]
    )

    async for event in agent.run([{"role": "user", "content": "下单"}]):
        if isinstance(event, ApprovalPending):
            assert gate.respond(
                event.pending_id, ApprovalDecision(approved=False, reason="额度不足")
            )

    finished = None
    # 重新跑一轮拿事件（上一轮迭代未收集，仅验证回填内容）：
    agent = AgentLoop(
        make_client(_write_handler_pair(requests)), tools=[guarded(create_order_t, gate)]
    )
    async for event in agent.run([{"role": "user", "content": "下单"}]):
        if isinstance(event, ToolCallFinished):
            finished = event
        if isinstance(event, ApprovalPending):
            assert gate.respond(
                event.pending_id, ApprovalDecision(approved=False, reason="额度不足")
            )
    assert finished is not None
    assert finished.ok  # 拒绝不是错误，是治理结果
    payload = json.loads(finished.content)
    assert payload["approval"] == "denied" and "额度不足" in payload["message"]
    # 回填给模型的 tool 消息里是"未执行"
    tool_msg = next(m for m in requests[1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["approval"] == "denied"


@pytest.mark.asyncio
async def test_approval_wait_time_does_not_burn_tool_timeout() -> None:
    """人的思考时间不计 tool_timeout：决策慢到超过超时上限，执行照样成功。"""
    requests: list[httpx2.Request] = []
    gate = StreamApprovalGate()
    agent = AgentLoop(
        make_client(_write_handler_pair(requests)),
        tools=[guarded(create_order_t, gate)],
        config=LoopConfig(tool_timeout=0.1),
    )

    finished = None
    async for event in agent.run([{"role": "user", "content": "下单"}]):
        if isinstance(event, ToolCallFinished):
            finished = event
        if isinstance(event, ApprovalPending):
            asyncio.create_task(_respond_slowly(gate, event.pending_id))
    assert finished is not None and finished.ok


async def _respond_slowly(gate: StreamApprovalGate, pending_id: str) -> None:
    await asyncio.sleep(0.4)  # 远超 tool_timeout=0.1
    assert gate.respond(pending_id, ApprovalDecision(approved=True))


@pytest.mark.asyncio
async def test_parallel_round_mixes_read_and_suspended_write() -> None:
    """读工具照常完成（先出 Finished），写工具进挂起段，两者互不阻塞。"""
    requests: list[httpx2.Request] = []
    gate = StreamApprovalGate()

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "完成"}), chunk(usage=USAGE)])
        return sse_response(
            multi_tool_chunks(
                [
                    ("call_r", "fast_report", "{}"),
                    ("call_w", "create_order_t", '{"sku": "A1001"}'),
                ]
            )
        )

    agent = AgentLoop(make_client(handler), tools=[fast_report, guarded(create_order_t, gate)])
    events: list = []
    async for event in agent.run([{"role": "user", "content": "hi"}]):
        events.append(event)
        if isinstance(event, ApprovalPending):
            assert gate.respond(event.pending_id, ApprovalDecision(approved=True))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished[0].name == "fast_report"  # 只读完成先于审批段
    assert finished[1].name == "create_order_t"
    pending_events = [e for e in events if isinstance(e, ApprovalPending)]
    assert len(pending_events) == 1
    assert pending_events[0].call_id == "call_w"  # 与 Started 配对
    # 历史按调用顺序回填：fast_report 是 call_r，写结果跟在后面
    tool_msgs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2


@pytest.mark.parametrize("close_early", [False, True])
async def test_approval_waiters_released_on_completion_and_close(close_early):
    gate = StreamApprovalGate()
    agent = AgentLoop(
        make_client(_write_handler_pair([])), tools=[guarded(create_order_t, gate)]
    )
    stream = agent.run([{"role": "user", "content": "write"}])
    async for event in stream:
        if isinstance(event, ApprovalPending):
            if close_early:
                await stream.aclose()
                break
            gate.respond(event.pending_id, ApprovalDecision(True))
    assert not gate._waiters


async def test_non_replayable_write_does_not_retry_uncertain_result():
    from agent_core.llm import ToolCall
    from agent_core.tools import Tool

    calls = []

    async def write(args):
        calls.append(args.sku)
        raise ConnectionError("result lost after commit")

    original = create_order_t
    write_tool = Tool(
        "write", "write", original.params_model, write, risk=RISK_SINGLE_CONFIRM,
    )
    loop = AgentLoop(None, tools=[write_tool], config=LoopConfig(
        retry=ToolRetryPolicy(retries=2, backoff=0),
    ))
    _, ok = await loop._execute(ToolCall(id="w1", name="write", arguments='{"sku":"A1"}'))
    assert not ok
    assert calls == ["A1"]


async def test_anyio_disconnect_releases_approval_while_slow_tool_is_pending():
    import anyio
    from agent_core.llm import ToolCall

    gate = StreamApprovalGate()
    agent = AgentLoop(None, tools=[guarded(create_order_t, gate), slow_report])

    async def consume():
        async for _ in agent._tool_round([
            ToolCall(id="w1", name="create_order_t", arguments='{"sku":"A1"}'),
            ToolCall(id="r1", name="slow_report", arguments="{}"),
        ], []):
            pass

    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        await anyio.sleep(0.01)
        group.cancel_scope.cancel()
    assert not gate._waiters


async def test_cancellation_releases_all_parallel_approvals():
    gate = StreamApprovalGate()

    def handler(request):
        return sse_response(multi_tool_chunks([
            ("w1", "create_order_t", '{"sku":"A1001"}'),
            ("w2", "create_order_t", '{"sku":"B2002"}'),
        ]))

    agent = AgentLoop(make_client(handler), tools=[guarded(create_order_t, gate)])
    stream = agent.run([{"role": "user", "content": "write"}])
    async for event in stream:
        if isinstance(event, ApprovalPending):
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            break
    assert not gate._waiters
