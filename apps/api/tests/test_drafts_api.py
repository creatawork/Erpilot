import json

import httpx2
import pytest
from agent_core.approval import StreamApprovalGate
from agent_core.events import ApprovalPending
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import ProductRow, StockRow, init_db, make_engine
from erp_store.models import ProductStatus
from erp_store.repository import ErpRepository
from erpilot_api.main import create_app
from langgraph.checkpoint.memory import InMemorySaver
from mcp_erp.session_tools import SessionErpTools
from sqlalchemy.orm import Session


@pytest.fixture
def erp(tmp_path):
    path = tmp_path / "erp.db"
    engine = make_engine(path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            ProductRow(
                sku="A", name="商品", category="测试", price=100, status=ProductStatus.ON_SALE.value
            )
        )
        session.add(StockRow(sku="A", quantity=1000, warehouse="主仓"))
        session.commit()
    return path, engine, ErpRepository(engine)


async def host(tmp_path, erp, saver=None, draft=None):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())

    def llm(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])
        return sse_response(
            tool_call_chunks(
                "submit",
                "submit_order_draft",
                json.dumps(
                    {
                        "draft_id": draft["draft_id"],
                        "revision": draft["revision"],
                    }
                ),
            )
        )

    app = create_app(
        client_factory=lambda: make_client(llm),
        model="glm-5.3-flash",
        trace_dir=tmp_path,
        tools=provider.tools,
        session_erp_tools=provider,
        checkpointer=saver or InMemorySaver(),
    )
    return app, provider


async def pending(app):
    stream = app.state.service.stream_run("s1", "提交草稿")
    async for event in stream:
        if isinstance(event, ApprovalPending):
            await stream.aclose()
            return event
    pytest.fail("expected approval")


async def test_http_crud_versions_validation_and_session_isolation(tmp_path, erp):
    app, _ = await host(tmp_path, erp)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/runtime")).json()["drafts_enabled"] is True
        body = {"customer": "客户", "items": [{"sku": "A", "quantity": 9}]}
        response = await client.post("/api/sessions/s1/order-drafts", json=body)
        assert response.status_code == 201
        draft = response.json()
        route = f"/api/sessions/s1/order-drafts/{draft['draft_id']}"
        body.update(expected_revision=1, items=[{"sku": "A", "quantity": 10}])
        revised = await client.patch(route, json=body)
        assert revised.json()["revision"] == 2 and revised.json()["total_amount"] == 980
        old = (await client.get(route + "?revision=1")).json()
        assert old["total_amount"] == 900 and old["status"] == "superseded"
        stale = await client.patch(route, json=body)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "draft_changed"
        assert (await client.get(route.replace("/s1/", "/s2/"))).status_code == 404
        assert (await client.get("/api/sessions/s2/order-drafts")).json() == {"items": []}
        assert (await client.get("/api/sessions/s1/order-drafts")).json()["items"] == [
            revised.json()
        ]
        body["items"][0]["quantity"] = True
        invalid = await client.post("/api/sessions/s1/order-drafts", json=body)
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_argument"
    assert erp[2].get_stock("A").quantity == 1000 and erp[2].count_orders(customer="客户") == 0


async def test_disabled_drafts_return_structured_error(tmp_path):
    app = create_app(tools=[], trace_dir=tmp_path, checkpointer=InMemorySaver())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/sessions/s1/order-drafts")
        assert response.status_code == 503
        assert set(response.json()["error"]) == {"code", "message", "hint"}


@pytest.mark.parametrize("approved", [False, True])
async def test_restored_host_hydrates_exact_version_and_approval(tmp_path, erp, approved):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())
    draft = provider.drafts.prepare("s1", "客户", [("A", 10)])
    saver = InMemorySaver()
    app, _ = await host(tmp_path, erp, saver, draft)
    approval = await pending(app)
    restored, _ = await host(tmp_path, erp, saver, draft)
    state = await restored.state.service.session_state("s1")
    assert state["pending_approvals"][0]["draft"] == draft
    assert state["pending_approvals"][0]["arguments"] == approval.arguments
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(restored), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat/approve/stream",
            json={
                "session_id": "s1",
                "pending_id": approval.pending_id,
                "approved": approved,
            },
        )
        assert response.status_code == 200 and "event: done" in response.text
    assert erp[2].count_orders(customer="客户") == int(approved)
    assert erp[2].get_stock("A").quantity == (990 if approved else 1000)
    assert provider.drafts.get("s1", draft["draft_id"])["status"] == (
        "submitted" if approved else "open"
    )


async def test_stale_approval_keeps_original_summary_and_cannot_commit(tmp_path, erp):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())
    draft = provider.drafts.prepare("s1", "客户", [("A", 9)])
    app, _ = await host(tmp_path, erp, draft=draft)
    approval = await pending(app)
    provider.drafts.revise("s1", draft["draft_id"], 1, "客户", [("A", 10)])
    state = await app.state.service.session_state("s1")
    assert state["pending_approvals"][0]["draft"]["total_amount"] == 900
    assert state["pending_approvals"][0]["draft"]["status"] == "superseded"
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await app.state.service.session_state("s1")
    assert "draft_changed" in state["tool_results"][0]["content"]
    assert erp[2].count_orders(customer="客户") == 0 and erp[2].get_stock("A").quantity == 1000


async def test_committed_response_loss_recovers_without_second_order(tmp_path, erp):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())
    draft = provider.drafts.prepare("s1", "客户", [("A", 10)])
    saver = InMemorySaver()
    app, _ = await host(tmp_path, erp, saver, draft)
    approval = await pending(app)
    original = provider.drafts.submit(
        "s1", draft["draft_id"], 1, approval.arguments["client_token"]
    )
    restored, _ = await host(tmp_path, erp, saver, draft)
    events = [
        event
        async for event in restored.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await restored.state.service.session_state("s1")
    assert original.order_id in state["tool_results"][0]["content"]
    assert erp[2].count_orders(customer="客户") == 1 and erp[2].get_stock("A").quantity == 990


async def test_price_drift_after_approval_requires_new_quote(tmp_path, erp):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())
    draft = provider.drafts.prepare("s1", "客户", [("A", 10)])
    app, _ = await host(tmp_path, erp, draft=draft)
    approval = await pending(app)
    with Session(erp[1]) as session:
        session.get(ProductRow, "A").price = 200
        session.commit()
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await app.state.service.session_state("s1")
    assert "quote_changed" in state["tool_results"][0]["content"]
    assert erp[2].count_orders(customer="客户") == 0 and erp[2].get_stock("A").quantity == 1000


async def test_model_forged_foreign_draft_cannot_hydrate_or_submit(tmp_path, erp):
    provider = await SessionErpTools.create(erp[0], writes=True, gate=StreamApprovalGate())
    foreign = provider.drafts.prepare("foreign-session", "私有客户", [("A", 10)])
    app, _ = await host(tmp_path, erp, draft=foreign)
    approval = await pending(app)
    state = await app.state.service.session_state("s1")
    assert "draft" not in state["pending_approvals"][0]
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await app.state.service.session_state("s1")
    assert "not_found" in state["tool_results"][0]["content"]
    assert "私有客户" not in json.dumps(state, ensure_ascii=False)
    assert erp[2].count_orders(customer="私有客户") == 0 and erp[2].get_stock("A").quantity == 1000
