import json

import pytest
from agent_core.graph import build_graph
from agent_core.graph_state import initial_state
from agent_core.recovery import MutationLookup
from agent_core.runtime_config import LoopConfig
from agent_core.tools import Tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel
from test_graph_approval import Client, WriteParams


async def test_business_commit_before_result_checkpoint_replays_original_token():
    class FailingSaver(InMemorySaver):
        fail = False

        async def aput(self, config, checkpoint, metadata, new_versions):
            if self.fail and checkpoint["channel_values"].get("tool_results"):
                self.fail = False
                raise RuntimeError("result checkpoint unavailable")
            return await super().aput(config, checkpoint, metadata, new_versions)

    saver = FailingSaver()
    received, committed = [], {}

    async def handler(args):
        received.append(args.model_dump())
        committed.setdefault(args.client_token, {"written": args.value})
        saver.fail = True
        return committed[args.client_token]

    tools = [Tool("write", "write", WriteParams, handler, risk="confirm", retry_safe=True)]
    config = {"configurable": {"thread_id": "crash"}}
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    pending = (await graph.ainvoke(initial_state([]), config))["__interrupt__"][0].value
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await graph.ainvoke(
            Command(resume={"pending_id": pending["pending_id"], "approved": True}), config
        )
    assert received == [pending["arguments"]]
    # Saver pending writes can recover a completed node without replaying its handler.
    saver.fail = False
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    result = await graph.ainvoke(None, config)
    assert result["completed"]
    assert len(committed) == 1
    assert all(args == pending["arguments"] for args in received)


class Reconciler:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def lookup(self, tool_name, arguments, client_token):
        self.calls.append((tool_name, arguments, client_token))
        if self.error:
            raise self.error
        return self.result


async def _approve_with_reconciler(thread_id, reconciler, handler):
    saver = InMemorySaver()
    tools = [Tool("write", "write", WriteParams, handler, risk="confirm", retry_safe=True)]
    graph = build_graph(
        Client(), tools, LoopConfig(), saver, approval_enabled=True,
        mutation_reconciler=reconciler,
    )
    config = {"configurable": {"thread_id": thread_id}}
    pending = (await graph.ainvoke(initial_state([]), config))["__interrupt__"][0].value
    result = await graph.ainvoke(
        Command(resume={"pending_id": pending["pending_id"], "approved": True}),
        config,
    )
    return pending, result, await graph.aget_state(config)


async def test_reconcile_found_returns_stored_result_without_calling_handler():
    async def handler(args):
        pytest.fail("a committed token must not invoke the write handler")

    reconciler = Reconciler(MutationLookup("found", {"written": 7}))
    pending, result, snapshot = await _approve_with_reconciler("found", reconciler, handler)
    call = reconciler.calls[0]
    history = result["tool_history"][-1]

    assert call[0] == "write"
    assert call[1] == {"value": 7}
    assert call[2] == pending["arguments"]["client_token"]
    assert history["invocation_status"] == "succeeded"
    assert json.loads(history["content"]) == {"written": 7}


async def test_reconcile_absent_runs_original_call_once_with_original_token():
    received = []

    async def handler(args):
        received.append(args.model_dump())
        return {"written": args.value}

    reconciler = Reconciler(MutationLookup("absent"))
    pending, _result, snapshot = await _approve_with_reconciler("absent", reconciler, handler)
    result = snapshot.values

    assert received == [pending["arguments"]]
    assert result["tool_history"][-1]["invocation_status"] == "succeeded"


@pytest.mark.parametrize(
    ("lookup", "error_code"),
    [
        (MutationLookup("conflict"), "idempotency_conflict"),
        (None, "reconciliation_unavailable"),
    ],
)
async def test_reconcile_conflict_or_failure_marks_unknown_without_write(lookup, error_code):
    async def handler(args):
        pytest.fail("unknown reconciliation state must never execute a write")

    reconciler = (
        Reconciler(error=RuntimeError("database unavailable"))
        if error_code == "reconciliation_unavailable"
        else Reconciler(lookup)
    )
    _pending, result, snapshot = await _approve_with_reconciler("unknown", reconciler, handler)
    result = snapshot.values
    history = result["tool_results"][-1]

    assert history["invocation_status"] == "unknown"
    assert json.loads(history["content"])["error"]["code"] == error_code
    assert result["step"] == 1
    assert result["recovery_required"] is True
    assert snapshot.tasks[0].interrupts[0].value["kind"] == "reconciliation_required"


async def test_reconcile_rejects_changed_tool_schema_before_lookup_or_write():
    class UpdatedWriteParams(BaseModel):
        value: int
        note: str | None = None
        client_token: str | None = None

    async def handler(args):
        pytest.fail("a changed tool schema cannot execute a checkpointed approval")

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "schema-change"}}
    original = build_graph(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm")],
        LoopConfig(),
        saver,
        approval_enabled=True,
    )
    pending = (await original.ainvoke(initial_state([]), config))["__interrupt__"][0].value
    reconciler = Reconciler(MutationLookup("absent"))
    updated = build_graph(
        Client(),
        [Tool("write", "write", UpdatedWriteParams, handler, risk="confirm")],
        LoopConfig(),
        saver,
        approval_enabled=True,
        mutation_reconciler=reconciler,
    )

    await updated.ainvoke(
        Command(resume={"pending_id": pending["pending_id"], "approved": True}), config
    )

    assert reconciler.calls == []
    snapshot = await updated.aget_state(config)
    assert snapshot.values["tool_results"][-1]["invocation_status"] == "unknown"


async def test_explicit_reconciliation_retry_rechecks_same_token_and_recovers_result():
    class FlakyReconciler:
        def __init__(self):
            self.calls = 0

        async def lookup(self, tool_name, arguments, client_token):
            self.calls += 1
            if self.calls == 1:
                return MutationLookup("conflict")
            return MutationLookup("found", {"written": 7})

    async def handler(args):
        pytest.fail("retry must query the existing token before any write")

    saver = InMemorySaver()
    reconciler = FlakyReconciler()
    config = {"configurable": {"thread_id": "retry-unknown"}}
    graph = build_graph(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm")],
        LoopConfig(), saver, approval_enabled=True, mutation_reconciler=reconciler,
    )
    pending = (await graph.ainvoke(initial_state([]), config))["__interrupt__"][0].value
    first = await graph.ainvoke(
        Command(resume={"pending_id": pending["pending_id"], "approved": True}), config
    )
    assert first["__interrupt__"][0].value["kind"] == "reconciliation_required"
    call_id = first["__interrupt__"][0].value["call_id"]
    result = await graph.ainvoke(
        Command(resume={"call_id": call_id, "retry": True}), config
    )

    assert result["tool_history"][-1]["invocation_status"] == "succeeded"
    assert json.loads(result["tool_history"][-1]["content"]) == {"written": 7}
    assert reconciler.calls == 2
