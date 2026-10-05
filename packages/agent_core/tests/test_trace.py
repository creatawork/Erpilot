"""trace 单测：JSONL 记录齐全、事件原样透传、异常留痕、可回放（transcript/摘要）。"""

import json
from pathlib import Path

import httpx2
import pytest
from agent_core.llm import TextDelta, Usage
from agent_core.loop import (
    AgentLoop,
    LoopEnd,
    StepEnd,
    StepStarted,
    ToolCall,
    ToolCallFinished,
    ToolCallStarted,
)
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.tools import tool
from agent_core.trace import (
    JsonlTraceRecorder,
    format_transcript,
    load_records,
    new_trace_path,
    summarize,
)
from openai import BadRequestError
from pydantic import BaseModel


class OrderQuery(BaseModel):
    order_id: str


@tool(name="get_order_status", description="查询订单状态", params=OrderQuery)
async def get_order_status(params: OrderQuery) -> dict[str, str]:
    return {"status": "已发货", "order_id": params.order_id}


def _handler_pair(requests: list[httpx2.Request], *, first_args: str):
    """两段式端点：首轮返回工具调用，见到 tool 消息后返回最终文本。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response(
                [
                    chunk(delta={"content": "订单 123 已发货"}),
                    chunk(usage=USAGE),
                ]
            )
        return sse_response(tool_call_chunks("call_1", "get_order_status", first_args))

    return handler


async def _recorded_run(tmp_path, handler) -> tuple[list, list]:
    agent = AgentLoop(make_client(handler), tools=[get_order_status])
    messages: list = [{"role": "user", "content": "查订单 123 的状态"}]
    recorder = JsonlTraceRecorder(tmp_path / "run.jsonl", model="glm-5.3-flash")
    events = [e async for e in recorder.run(agent, messages)]
    return events, load_records(tmp_path / "run.jsonl")


@pytest.mark.asyncio
async def test_records_full_pipeline_and_passes_events_through(tmp_path) -> None:
    """两步任务：六类记录按序齐全，事件流与裸跑 agent.run 完全一致。"""
    requests: list[httpx2.Request] = []
    handler = _handler_pair(requests, first_args='{"order_id": "123"}')

    events, records = await _recorded_run(tmp_path, handler)

    assert [r["type"] for r in records] == [
        "run_start",
        "step_start",
        "step_end",
        "tool_call",
        "step_start",
        "step_end",
        "run_end",
    ]
    # 事件透传：recorder 只是旁观者——除轮次边界事件（含非确定耗时）外逐个相等
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
        LoopEnd(steps=2, usage=Usage(26, 14, 40), completed=True),
    ]
    assert [e.step for e in events if isinstance(e, StepStarted)] == [1, 2]

    run_start = records[0]
    assert run_start["model"] == "glm-5.3-flash"
    assert run_start["messages"] == [{"role": "user", "content": "查订单 123 的状态"}]

    tool_call = next(r for r in records if r["type"] == "tool_call")
    assert tool_call["step"] == 1
    assert tool_call["id"] == "call_1"
    assert tool_call["name"] == "get_order_status"
    assert json.loads(tool_call["arguments"]) == {"order_id": "123"}
    assert json.loads(tool_call["content"])["status"] == "已发货"
    assert tool_call["ok"] is True
    assert tool_call["duration_ms"] >= 0

    final_step = [r for r in records if r["type"] == "step_end"][-1]
    assert final_step["text"] == "订单 123 已发货"
    assert final_step["usage"] == dict(USAGE)
    assert final_step["cost"] is not None and final_step["cost"] > 0  # 价目表已收录

    run_end = records[-1]
    assert run_end["steps"] == 2 and run_end["completed"] is True
    assert run_end["usage"] == {"prompt_tokens": 26, "completion_tokens": 14, "total_tokens": 40}
    assert run_end["cost"] is not None and run_end["duration_ms"] >= 0
    # 结束快照含完整历史（user → assistant(tool_calls) → tool → assistant）
    assert [m["role"] for m in run_end["messages"]] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_transcript_replay_restores_conversation(tmp_path) -> None:
    _, records = await _recorded_run(tmp_path, _handler_pair([], first_args='{"order_id": "123"}'))

    text = format_transcript(records)

    assert "查订单 123 的状态" in text  # 用户提问
    assert "get_order_status" in text  # 工具调用与入参
    assert "订单 123 已发货" in text  # 模型最终回答
    assert "✓" in text and "结束：2 步" in text and "tok" in text


@pytest.mark.asyncio
async def test_exception_is_recorded_then_reraised(tmp_path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "bad request"}})

    agent = AgentLoop(make_client(handler), tools=[get_order_status])
    recorder = JsonlTraceRecorder(tmp_path / "bad.jsonl", model="glm-5.3-flash")

    with pytest.raises(BadRequestError):  # openai SDK 对 4xx 不重试，直接抛
        [e async for e in recorder.run(agent, [{"role": "user", "content": "hi"}])]

    records = load_records(tmp_path / "bad.jsonl")
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_error"
    assert "bad request" in records[-1]["error"]


@pytest.mark.asyncio
async def test_summarize_and_new_trace_path(tmp_path) -> None:
    _, records = await _recorded_run(tmp_path, _handler_pair([], first_args='{"order_id": "123"}'))
    summary = summarize(records)
    assert summary is not None
    assert summary.steps == 2 and summary.completed is True
    assert summary.usage == Usage(26, 14, 40) and summary.cost > 0

    path = new_trace_path(tmp_path, "glm-5.3-flash")
    assert path.parent == tmp_path and path.suffix == ".jsonl"


@pytest.mark.asyncio
async def test_recorder_appends_and_creates_dirs(tmp_path) -> None:
    """同一个文件可追加多次 run（按行流水，不覆盖），深层目录自动创建。"""
    target = tmp_path / "a" / "b" / "run.jsonl"
    for _ in range(2):
        agent = AgentLoop(
            make_client(_handler_pair([], first_args='{"order_id": "1"}')), tools=[get_order_status]
        )
        recorder = JsonlTraceRecorder(target, model="glm-5.3-flash")
        [e async for e in recorder.run(agent, [{"role": "user", "content": "hi"}])]

    records = load_records(target)
    assert [r["type"] for r in records].count("run_start") == 2
    assert [r["type"] for r in records].count("run_end") == 2


# ---- 审批事件留痕（M4 第 2 周，ADR-0006） ----


@pytest.mark.asyncio
async def test_recorder_writes_approval_records(tmp_path: Path) -> None:
    """挂起式审批的 pending/resolved 都进 trace，回放文本可读。"""
    from agent_core.approval import (
        RISK_SINGLE_CONFIRM,
        ApprovalDecision,
        StreamApprovalGate,
        guarded,
    )
    from agent_core.loop import AgentLoop, ApprovalPending
    from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
    from pydantic import BaseModel

    class WriteInput(BaseModel):
        sku: str

    @tool(name="create_order_x", description="建单", params=WriteInput, risk=RISK_SINGLE_CONFIRM)
    async def create_order_x(params: WriteInput) -> dict[str, str]:
        return {"created": params.sku}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "已创建"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks("call_w", "create_order_x", '{"sku": "A1001"}'))

    gate = StreamApprovalGate()
    agent = AgentLoop(make_client(handler), tools=[guarded(create_order_x, gate)])
    path = tmp_path / "approval.jsonl"
    recorder = JsonlTraceRecorder(path, "glm-5.3-flash")

    async for event in recorder.run(agent, [{"role": "user", "content": "下单"}]):
        if isinstance(event, ApprovalPending):
            assert gate.respond(
                event.pending_id, ApprovalDecision(approved=False, reason="测试拒绝")
            )

    records = load_records(path)
    kinds = [r["type"] for r in records]
    assert "approval_pending" in kinds and "approval_resolved" in kinds
    pend = next(r for r in records if r["type"] == "approval_pending")
    assert pend["tool"] == "create_order_x" and pend["risk"] == RISK_SINGLE_CONFIRM
    assert pend["arguments"] == {"sku": "A1001"}
    res = next(r for r in records if r["type"] == "approval_resolved")
    assert res["approved"] is False and res["reason"] == "测试拒绝"

    transcript = format_transcript(records)
    assert "待审批" in transcript and "拒绝" in transcript
