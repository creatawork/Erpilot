import httpx2
from agent_core.testing import chunk, make_client, sse_response
from agent_core.tools import Tool
from erpilot_api.main import create_app
from pydantic import BaseModel


class Args(BaseModel):
    sku: str
    delta: int


async def handler(args):
    raise AssertionError("API lifecycle test must never write business data")


def setup(tmp_path, ttl=1800):
    app = create_app(
        trace_dir=tmp_path, client_factory=lambda: make_client(
            lambda _: sse_response([chunk(delta={"content": "answer"})]),
        ), tools=[Tool(name="adjust_stock", description="adjust", params_model=Args,
                       handler=handler, risk="low", schema_version="v1")],
    )
    store = app.state.runs.store
    run = store.create_run("s", "adjust", [], None)
    store.record_write_intent(
        run, "c", session_id="s", tool="adjust_stock", tool_schema_version="v1",
        arguments={"sku": "A1001", "delta": 3}, client_token="t", pending_id="p",
        ttl_seconds=ttl,
    )
    return app, run


async def test_approval_api_repeats_and_exposes_conflict_and_ownership(tmp_path):
    app, run = setup(tmp_path)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test",
    ) as client:
        request = {"pending_id": "p", "approved": True, "run_id": run, "expected_version": 1}
        wrong_run = await client.post("/api/chat/approve", json={**request, "run_id": "wrong"})
        assert wrong_run.status_code == 409
        assert (await client.post("/api/chat/approve", json=request)).json()["ok"] is True
        assert (await client.post("/api/chat/approve", json=request)).json()["ok"] is True
        conflict = await client.post("/api/chat/approve", json={**request, "approved": False})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["approval"]["status"] == "approved"
        unknown = await client.post("/api/chat/approve", json={**request, "pending_id": "missing"})
        assert unknown.status_code == 404
        snapshot = (await client.get(f"/api/runs/{run}")).json()
        assert snapshot["pending"] == []
        assert snapshot["approvals"][0]["status"] == "approved"
        assert len((await client.get("/api/sessions/s/runs")).json()["runs"]) == 1
        assert (await client.get("/api/runs/missing")).status_code == 404


async def test_expired_and_cancelled_api_approvals_never_execute(tmp_path):
    app, run = setup(tmp_path, ttl=-1)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test",
    ) as client:
        approval = await client.post("/api/chat/approve", json={"pending_id": "p", "approved": True,
                                                              "run_id": run})
        assert approval.status_code == 410
        assert app.state.runs.store.get_approval("p")["status"] == "expired"
        result = await client.post(f"/api/runs/{run}/cancel")
        assert result.json()["status"] == "cancelled"


async def test_new_run_api_returns_snapshot_then_cursor_stream(tmp_path):
    app = create_app(trace_dir=tmp_path, tools=[], client_factory=lambda: make_client(
        lambda _: sse_response([chunk(delta={"content": "answer"})]),
    ))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/runs", json={"session_id": "s", "message": "question"})
        created = response.json()
        run = created["run_id"]
        await app.state.runs.wait(run)
        final = (await client.get(f"/api/runs/{run}")).json()
        assert final["final_answer"] == "answer"
        assert final["status"] == "completed"
        response = await client.get(f"/api/runs/{run}/events?after_seq={created['last_seq']}")
        assert 'event: done' in response.text
        assert (await client.post(f"/api/runs/{run}/resume")).json()["status"] == "completed"
