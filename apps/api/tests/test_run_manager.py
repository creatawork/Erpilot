import asyncio
import json

from agent_core.demo_tools import DEMO_TOOLS
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erpilot_api.execution import RunManager
from erpilot_api.run_store import RunStore
from erpilot_api.service import ChatService


def manager(tmp_path, requests):
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks("c", "get_order_status", '{"order_id":"123"}'))
    store = RunStore(tmp_path / "runs.db")
    service = ChatService(client_factory=lambda: make_client(handler), model="glm-5.3-flash",
                          tools=DEMO_TOOLS, trace_dir=tmp_path, run_store=store)
    return RunManager(service, store)


async def test_subscription_close_does_not_cancel_task_and_cursor_replays(tmp_path):
    requests = []
    runs = manager(tmp_path, requests)
    run_id = runs.start("s", "query")
    subscription = runs.events(run_id)
    first = await anext(subscription)
    await subscription.aclose()
    await runs.wait(run_id)
    snapshot = runs.store.snapshot(run_id)
    assert snapshot["status"] == "completed"
    assert snapshot["final_answer"] == "done"
    assert len(requests) == 2
    replay = [e async for e in runs.events(run_id, first["data"]["seq"])]
    assert all(e["data"]["seq"] > first["data"]["seq"] for e in replay)
    assert [e["data"]["seq"] for e in replay] == list(range(2, snapshot["last_seq"] + 1))
    assert replay[-1]["event"] == "done"


async def test_duplicate_resume_has_only_one_execution(tmp_path):
    requests = []
    runs = manager(tmp_path, requests)
    run_id = runs.store.create_run("s", "query", [{"role": "system", "content": "rules"}], None)
    await asyncio.gather(runs.resume(run_id), runs.resume(run_id))
    await runs.wait(run_id)
    assert len(requests) == 2
    assert runs.store.snapshot(run_id)["status"] == "completed"


def test_claim_competes_and_expired_owner_is_recoverable(tmp_path):
    from datetime import UTC, datetime, timedelta
    now = datetime(2026, 10, 6, tzinfo=UTC)
    store = RunStore(tmp_path / "runs.db", clock=lambda: now)
    run = store.create_run("s", "query", [], None)
    assert store.claim_run(run, "first")
    assert not store.claim_run(run, "second")
    now += timedelta(seconds=31)
    assert store.claim_run(run, "second")
    store.release_owner(run, "first")
    assert store.get_run(run)["execution_owner"] == "second"


async def test_snapshot_to_subscription_boundary_has_no_gap(tmp_path):
    runs = manager(tmp_path, [])
    run = runs.store.create_run("s", "query", [], None)
    before = runs.store.snapshot(run)
    runs.store.append_event(run, "delta", {"text": "between"})
    runs.store.finish_run(run, [], "between")
    runs.store.append_event(run, "done", {"completed": True})
    replay = [e async for e in runs.events(run, before["last_seq"])]
    assert [e["event"] for e in replay] == ["delta", "done"]


async def test_shutdown_preserves_running_checkpoint_for_resume(tmp_path):
    from agent_core.tools import Tool
    from pydantic import BaseModel

    class Args(BaseModel):
        pass

    entered = asyncio.Event()
    async def slow(args):
        entered.set()
        await asyncio.Event().wait()

    def handler(request):
        return sse_response(tool_call_chunks("c", "slow", "{}"))

    store = RunStore(tmp_path / "runs.db")
    service = ChatService(client_factory=lambda: make_client(handler), model="glm-5.3-flash",
                          trace_dir=tmp_path, tools=[Tool(name="slow", description="slow",
                          params_model=Args, handler=slow)], run_store=store)
    runs = RunManager(service, store)
    run = runs.start("s", "query")
    await asyncio.wait_for(entered.wait(), 5)
    await runs.close()
    reopened = RunStore(store.path)
    assert reopened.get_run(run)["status"] == "recovering"
    assert reopened.get_session("s")["active_run_id"] == run
    assert reopened.get_run(run)["execution_owner"] is None
    assert reopened.get_run(run)["messages"][-1]["tool_calls"][0]["id"] == "c"


async def test_expired_lease_does_not_replace_local_running_owner(tmp_path):
    from datetime import UTC, datetime, timedelta

    runs = manager(tmp_path, [])
    now = datetime(2026, 10, 6, tzinfo=UTC)
    runs.store._clock = lambda: now
    run = runs.start("s", "query")
    owner = runs.store.get_run(run)["execution_owner"]
    first = runs._tasks[run]
    now += timedelta(seconds=31)
    await runs.resume(run)
    assert runs._tasks[run] is first
    assert runs.store.get_run(run)["execution_owner"] == owner
    await runs.wait(run)
