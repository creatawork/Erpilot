import asyncio
import json
from datetime import UTC, datetime, timedelta

from agent_core.graph_tools import execute_tool_call, prepare_tool_calls
from agent_core.llm import ToolCall
from agent_core.runtime_config import LoopConfig, ToolRetryPolicy
from agent_core.tools import Tool
from pydantic import BaseModel


class Params(BaseModel):
    value: int


async def test_validation_and_unknown_tools_never_invoke_handler():
    invoked = []

    async def handler(args):
        invoked.append(args)

    tools = {"read": Tool("read", "read", Params, handler)}
    calls = prepare_tool_calls(
        [
            ToolCall("a", "read", '{"value":"bad"}'),
            ToolCall("b", "missing", "{}"),
        ],
        tools,
    )
    results = [await execute_tool_call(c, tools, LoopConfig()) for c in calls]
    assert [r["call_id"] for r in results] == ["a", "b"]
    assert [json.loads(r["content"])["error"]["type"] for r in results] == [
        "validation",
        "unknown_tool",
    ]
    assert not invoked


async def test_normalizes_arguments_and_retries_transient_errors():
    attempts = []

    async def handler(args):
        attempts.append(args.value)
        if len(attempts) == 1:
            raise TimeoutError()
        return {"value": args.value}

    tools = {"read": Tool("read", "read", Params, handler)}
    call = prepare_tool_calls([ToolCall("c", "read", '{"value":"7"}')], tools)[0]
    assert call["arguments"] == {"value": 7}
    result = await execute_tool_call(call, tools, LoopConfig(retry=ToolRetryPolicy(backoff=0)))
    assert result["ok"] is True
    assert json.loads(result["content"]) == {"value": 7}
    assert attempts == [7, 7]


async def test_read_tools_fan_out_and_preserve_call_pairing():
    from agent_core.graph import build_graph
    from agent_core.graph_state import initial_state
    from agent_core.llm import StreamEnd, TextDelta
    from langgraph.checkpoint.memory import InMemorySaver

    entered = asyncio.Event()

    async def handler(args):
        if args.value == 1:
            await asyncio.wait_for(entered.wait(), 1)
        else:
            entered.set()
        return args.value

    class Client:
        async def stream_chat(self, messages, tools=None):
            if messages and messages[-1]["role"] == "tool":
                yield TextDelta("done")
            else:
                yield ToolCall("a", "read", '{"value":1}')
                yield ToolCall("b", "read", '{"value":2}')
            yield StreamEnd()

    graph = build_graph(
        Client(), [Tool("read", "read", Params, handler)], LoopConfig(), InMemorySaver()
    )
    state = await graph.ainvoke(initial_state([]), {"configurable": {"thread_id": "reads"}})
    assert [
        (m["tool_call_id"], m["content"]) for m in state["messages"] if m["role"] == "tool"
    ] == [
        ("a", "1"),
        ("b", "2"),
    ]
class WriteParams(BaseModel):
    value: int
    client_token: str | None = None


def test_prepare_write_call_records_immutable_schema_and_approval_deadline():
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    calls = [ToolCall("call-1", "write", '{"value":7}')]
    tools = {
        "write": Tool(
            "write", "write", WriteParams, lambda args: None, risk="confirm"
        )
    }

    [prepared] = prepare_tool_calls(
        calls, tools, now=now, approval_ttl_seconds=60
    )

    assert prepared["client_token"] == prepared["arguments"]["client_token"]
    assert prepared["tool_schema_version"]
    assert prepared["arguments_fingerprint"]
    assert prepared["approval_created_at"] == now.isoformat()
    assert prepared["approval_expires_at"] == (now + timedelta(seconds=60)).isoformat()
    assert prepared["approval_status"] == "pending"
    assert prepared["invocation_status"] == "waiting_approval"


def test_prepare_write_rejects_naive_clock_and_nonpositive_ttl():
    tools = {
        "write": Tool(
            "write", "write", WriteParams, lambda args: None, risk="confirm"
        )
    }
    calls = [ToolCall("call-1", "write", '{"value":7}')]

    try:
        prepare_tool_calls(
            calls,
            tools,
            now=datetime(2026, 10, 8),
            approval_ttl_seconds=60,
        )
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive datetime must be rejected")

    try:
        prepare_tool_calls(
            calls,
            tools,
            now=datetime(2026, 10, 8, tzinfo=UTC),
            approval_ttl_seconds=0,
        )
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("non-positive TTL must be rejected")
