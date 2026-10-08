import json
from datetime import UTC, datetime, timedelta

import pytest
from agent_core.graph import build_graph
from agent_core.graph_approval import ResumeDecision
from agent_core.graph_state import initial_state
from agent_core.llm import StreamEnd, TextDelta, ToolCall
from agent_core.runtime_config import LoopConfig
from agent_core.tools import Tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ValidationError


class WriteParams(BaseModel):
    value: int
    client_token: str | None = None


class Client:
    async def stream_chat(self, messages, tools=None):
        if messages and messages[-1]["role"] == "tool":
            yield TextDelta("done")
        else:
            yield ToolCall("w", "write", '{"value":7}')
        yield StreamEnd()


def test_write_tools_require_approval_runtime():
    async def handler(args):
        pytest.fail("write must never run")

    with pytest.raises(ValueError, match="approval"):
        build_graph(
            Client(),
            [Tool("write", "write", WriteParams, handler, risk="confirm")],
            LoopConfig(),
            InMemorySaver(),
        )


@pytest.mark.parametrize("approved", [False, True])
async def test_interrupt_payload_and_decision(approved):
    received = []

    async def handler(args):
        received.append(args.model_dump())
        return {"written": args.value}

    saver = InMemorySaver()
    tools = [Tool("write", "write", WriteParams, handler, risk="confirm", retry_safe=True)]
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    config = {"configurable": {"thread_id": "approve"}}
    result = await graph.ainvoke(initial_state([]), config)
    payload = result["__interrupt__"][0].value
    token = payload["arguments"]["client_token"]
    assert token and payload["call_id"] == "w" and payload["tool"] == "write"
    assert received == []
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    result = await graph.ainvoke(
        Command(
            resume={
                "pending_id": payload["pending_id"],
                "approved": approved,
                "reason": "decision",
            }
        ),
        config,
    )
    assert result["completed"] is True
    if approved:
        assert received == [{"value": 7, "client_token": token}]
    else:
        assert received == []
        tool_message = next(m for m in result["messages"] if m["role"] == "tool")
        assert json.loads(tool_message["content"])["approval"] == "denied"


async def test_write_retry_reuses_checkpointed_token():
    received = []
    committed = {}

    async def handler(args):
        received.append(args.model_dump())
        if args.client_token not in committed:
            committed[args.client_token] = {"written": args.value}
            raise TimeoutError("commit happened but response lost")
        return committed[args.client_token]

    graph = build_graph(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm", retry_safe=True)],
        LoopConfig(),
        InMemorySaver(),
        approval_enabled=True,
    )
    config = {"configurable": {"thread_id": "retry"}}
    result = await graph.ainvoke(initial_state([]), config)
    payload = result["__interrupt__"][0].value
    await graph.ainvoke(
        Command(resume={"pending_id": payload["pending_id"], "approved": True}), config
    )
    assert len(committed) == 1
    assert received == [payload["arguments"], payload["arguments"]]


async def test_wrong_pending_or_parameter_replacement_cannot_execute():
    async def handler(args):
        pytest.fail("invalid approval cannot execute")

    graph = build_graph(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm")],
        LoopConfig(),
        InMemorySaver(),
        approval_enabled=True,
    )
    config = {"configurable": {"thread_id": "stale"}}
    await graph.ainvoke(initial_state([]), config)
    with pytest.raises(ValueError):
        await graph.ainvoke(
            Command(resume={"pending_id": "wrong", "approved": True, "arguments": {"value": 9}}),
            config,
        )


async def test_approval_lifecycle_metadata_survives_checkpoint_reload():
    now = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
    saver = InMemorySaver()
    tools = [
        Tool("write", "write", WriteParams, lambda args: None, risk="confirm")
    ]
    config = {"configurable": {"thread_id": "lifecycle"}}
    graph = build_graph(
        Client(),
        tools,
        LoopConfig(),
        saver,
        approval_enabled=True,
        approval_ttl_seconds=75,
        clock=lambda: now,
    )

    await graph.ainvoke(initial_state([]), config)
    first_snapshot = await graph.aget_state(config)
    first_call = first_snapshot.values["pending_calls"][0]
    assert first_call["approval_created_at"] == now.isoformat()
    assert first_call["approval_expires_at"] == (now + timedelta(seconds=75)).isoformat()
    assert first_call["approval_status"] == "pending"
    assert first_call["invocation_status"] == "waiting_approval"
    assert first_call["tool_schema_version"]
    assert first_call["arguments_fingerprint"]

    restarted_graph = build_graph(
        Client(), tools, LoopConfig(), saver, approval_enabled=True,
        approval_ttl_seconds=75, clock=lambda: now + timedelta(days=1),
    )
    reloaded_snapshot = await restarted_graph.aget_state(config)
    reloaded_call = reloaded_snapshot.values["pending_calls"][0]
    assert reloaded_call["pending_id"] == first_call["pending_id"]
    assert reloaded_call["client_token"] == first_call["client_token"]
    assert reloaded_call["approval_expires_at"] == first_call["approval_expires_at"]


@pytest.mark.parametrize("outcome", ["expired", "cancelled"])
async def test_nonapproval_outcome_never_calls_write_handler(outcome):
    async def handler(args):
        pytest.fail(f"{outcome} approval must never execute a write")

    saver = InMemorySaver()
    graph = build_graph(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm")],
        LoopConfig(),
        saver,
        approval_enabled=True,
    )
    config = {"configurable": {"thread_id": outcome}}
    pending = (await graph.ainvoke(initial_state([]), config))["__interrupt__"][0].value

    result = await graph.ainvoke(
        Command(
            resume={
                "pending_id": pending["pending_id"],
                "approved": False,
                "outcome": outcome,
                "reason": "lifecycle decision",
            }
        ),
        config,
    )

    assert result["completed"]
    tool_history = result["tool_history"][-1]
    assert tool_history["approval_status"] == outcome
    assert tool_history["invocation_status"] == outcome
    assert json.loads(tool_history["content"])["approval"] == outcome


def test_approval_outcome_must_match_boolean_decision():
    with pytest.raises(ValidationError, match="outcome must match approved"):
        ResumeDecision.model_validate(
            {"pending_id": "pending-1", "approved": False, "outcome": "approved"}
        )
