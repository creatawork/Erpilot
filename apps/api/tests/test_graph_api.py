import json

import httpx2
import pytest
from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.events import ApprovalPending
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.tools import Tool
from erpilot_api.main import create_app
from erpilot_api.run_store import RunStore
from erpilot_api.service import ChatService
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel


class Params(BaseModel):
    sku: str
    client_token: str | None = None


def setup_service(tmp_path, saver, executed):
    async def handler(args):
        executed.append(args.model_dump())
        return {"created": args.sku}

    def llm(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks("w", "write", '{"sku":"A1001"}'))

    return dict(
        client_factory=lambda: make_client(llm),
        model="glm-5.3-flash",
        trace_dir=tmp_path,
        checkpointer=saver,
        tools=[Tool("write", "write", Params, handler, risk="confirm", retry_safe=True)],
        approval_gate=StreamApprovalGate(),
    )


async def pending_service(tmp_path, saver, executed):
    kwargs = setup_service(tmp_path, saver, executed)
    service = ChatService(**kwargs, run_store=RunStore(tmp_path / "runs.db"))
    stream = service.stream_run("s1", "write")
    async for event in stream:
        if isinstance(event, ApprovalPending):
            await stream.aclose()
            return event, kwargs
    pytest.fail("expected checkpointed approval")


async def test_disconnected_approval_survives_service_reconstruction(tmp_path):
    saver, executed = InMemorySaver(), []
    pending, kwargs = await pending_service(tmp_path, saver, executed)
    service = ChatService(**kwargs, run_store=RunStore(tmp_path / "runs.db"))
    state = await service.session_state("s1")
    assert state["status"] == "waiting_approval"
    assert state["pending_approvals"][0]["arguments"] == pending.arguments
    assert not service.respond_approval(pending.pending_id, ApprovalDecision(True))
    events = [e async for e in service.stream_resume("s1", pending.pending_id, True)]
    assert events[-1].completed
    assert executed == [pending.arguments]
    state = await service.session_state("s1")
    assert state["pending_approvals"] == []
    assert state["tool_results"][0]["call_id"] == pending.call_id
    assert state["tool_results"][0]["ok"] is True
    assert RunStore(tmp_path / "runs.db").get_session("s1")["history"][-1]["content"] == "done"


async def test_interrupted_model_node_can_be_retried_from_its_checkpoint(tmp_path):
    calls = 0

    def llm(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(503, json={"error": {"message": "temporary outage"}})
        return sse_response([chunk(delta={"content": "recovered"}), chunk(usage=USAGE)])

    run_store_path = tmp_path / "retry-runs.db"
    app = create_app(
        client_factory=lambda: make_client(llm, max_retries=0),
        model="glm-5.3-flash",
        trace_dir=tmp_path,
        tools=[],
        checkpointer=InMemorySaver(),
        run_store_path=run_store_path,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        failed = await client.post(
            "/api/chat/stream", json={"session_id": "retry", "message": "retry this"}
        )
        assert failed.status_code == 200 and "execution_error" in failed.text
        state = await client.get("/api/sessions/retry/state")
        assert state.json()["status"] == "interrupted"
        resumed = await client.post("/api/sessions/retry/resume/stream")
        assert resumed.status_code == 200 and "event: done" in resumed.text
        state = await client.get("/api/sessions/retry/state")
        assert state.json()["status"] == "completed"
    assert calls == 2
    assert RunStore(run_store_path).get_session("retry")["history"][-1]["content"] == "recovered"


async def test_approval_resume_rejects_unknown_pending(tmp_path):
    saver, executed = InMemorySaver(), []
    pending, kwargs = await pending_service(tmp_path, saver, executed)
    app = create_app(**kwargs)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        unknown = await client.get("/api/sessions/missing/state")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "session_not_found"
        stale = await client.post(
            "/api/chat/approve/stream",
            json={
                "session_id": "s1",
                "pending_id": "wrong",
                "approved": True,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_approval"
        approved = await client.post(
            "/api/chat/approve/stream",
            json={
                "session_id": "s1",
                "pending_id": pending.pending_id,
                "approved": False,
            },
        )
        assert approved.status_code == 200 and "event: done" in approved.text
        duplicate = await client.post(
            "/api/chat/approve/stream",
            json={
                "session_id": "s1",
                "pending_id": pending.pending_id,
                "approved": True,
            },
        )
        assert duplicate.status_code == 409
    assert executed == []
