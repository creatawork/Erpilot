import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from agent_core.approval import StreamApprovalGate
from agent_core.events import ApprovalPending
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import OrderRow, ProductRow, StockRow, init_db, make_engine
from erp_store.models import OrderStatus, ProductStatus
from erp_store.mutations import ErpMutations
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
    order = ErpMutations(engine).create_order("客户", [("A", 10)], note="原订单")
    return path, engine, ErpRepository(engine), order


async def host(tmp_path, erp, operation=None, *, kind="adjust_stock", saver=None, writes=True):
    provider = await SessionErpTools.create(
        erp[0], writes=writes, gate=StreamApprovalGate() if writes else None
    )

    def llm(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])
        return sse_response(
            tool_call_chunks(
                "execute", kind, json.dumps({"operation_id": operation["operation_id"]})
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
    stream = app.state.service.stream_run("s1", "执行已确认的操作")
    async for event in stream:
        if isinstance(event, ApprovalPending):
            await stream.aclose()
            return event
    pytest.fail("expected approval")


async def test_disabled_operations_return_structured_error_and_runtime_flag(tmp_path):
    app = create_app(tools=[], trace_dir=tmp_path, checkpointer=InMemorySaver())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        runtime = (await client.get("/api/runtime")).json()
        assert runtime.get("business_approvals_enabled") is False
        response = await client.get("/api/sessions/s1/operations/missing")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "business_approvals_disabled"
    assert set(response.json()["error"]) == {"code", "message", "hint"}
    assert response.json()["error"]["hint"]


async def test_http_operation_returns_exact_summary_and_current_validity(tmp_path, erp):
    app, provider = await host(tmp_path, erp)
    operation = provider.operations.prepare_adjust("s1", "A", -3)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/runtime")).json()["business_approvals_enabled"] is True
        route = f"/api/sessions/s1/operations/{operation['operation_id']}"
        assert (await client.get(route)).json() == operation
        foreign = await client.get(route.replace("/s1/", "/foreign/"))
        assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "not_found"
        with Session(erp[1]) as session:
            session.get(StockRow, "A").quantity = 950
            session.commit()
        changed = (await client.get(route)).json()
        assert changed["summary"] == operation["summary"]
        assert changed["status"] == "changed" and changed["invalid_reason"]


async def test_readonly_provider_does_not_advertise_business_approval(tmp_path, erp):
    app, _ = await host(tmp_path, erp, writes=False)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/runtime")).json()["business_approvals_enabled"] is False


async def test_real_mcp_preparation_is_readonly_and_snapshot_is_available_over_http(tmp_path, erp):
    app, provider = await host(tmp_path, erp)
    tools = {tool.name: tool for tool in provider.for_session("s1")}
    preparation = tools["prepare_stock_adjustment"]
    result = await preparation.handler(preparation.params_model(sku="A", delta=-3))
    operation = json.loads(result) if isinstance(result, str) else result
    assert operation["summary"]["quantity_after"] == 987
    assert erp[2].get_stock("A").quantity == 990
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/sessions/s1/operations/{operation['operation_id']}")
    assert response.status_code == 200 and response.json() == operation


@pytest.mark.parametrize("kind", ["cancel_order", "adjust_stock"])
@pytest.mark.parametrize("approved", [False, True])
async def test_rebuilt_host_hydrates_exact_operation_and_http_approval(
    tmp_path, erp, kind, approved
):
    _, provider = await host(tmp_path, erp)
    operation = (
        provider.operations.prepare_cancel("s1", erp[3].order_id)
        if kind == "cancel_order"
        else provider.operations.prepare_adjust("s1", "A", -3)
    )
    saver = InMemorySaver()
    app, _ = await host(tmp_path, erp, operation, kind=kind, saver=saver)
    approval = await pending(app)
    restored, _ = await host(tmp_path, erp, operation, kind=kind, saver=saver)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(restored), base_url="http://test"
    ) as client:
        state = (await client.get("/api/sessions/s1/state")).json()
        assert state["pending_approvals"][0]["operation"] == operation
        assert state["pending_approvals"][0]["arguments"] == approval.arguments
        response = await client.post(
            "/api/chat/approve/stream",
            json={"session_id": "s1", "pending_id": approval.pending_id, "approved": approved},
        )
        assert response.status_code == 200 and "event: done" in response.text
    assert erp[2].get_stock("A").quantity == (
        1000 if kind == "cancel_order" and approved else 987 if approved else 990
    )
    assert provider.operations.get("s1", operation["operation_id"])["status"] == (
        "executed" if approved else "open"
    )


@pytest.mark.parametrize("kind", ["cancel_order", "adjust_stock"])
async def test_changed_business_data_preserves_approval_summary_and_prevents_write(
    tmp_path, erp, kind
):
    _, provider = await host(tmp_path, erp)
    operation = (
        provider.operations.prepare_cancel("s1", erp[3].order_id)
        if kind == "cancel_order"
        else provider.operations.prepare_adjust("s1", "A", -3)
    )
    app, _ = await host(tmp_path, erp, operation, kind=kind)
    approval = await pending(app)
    with Session(erp[1]) as session:
        if kind == "cancel_order":
            session.get(OrderRow, erp[3].order_id).status = OrderStatus.PENDING_SHIPMENT.value
        else:
            session.get(StockRow, "A").quantity = 950
        session.commit()
    state = await app.state.service.session_state("s1")
    hydrated = state["pending_approvals"][0]["operation"]
    assert hydrated["summary"] == operation["summary"] and hydrated["status"] == "changed"
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await app.state.service.session_state("s1")
    assert "operation_changed" in state["tool_results"][0]["content"]
    assert erp[2].get_stock("A").quantity == (990 if kind == "cancel_order" else 950)


@pytest.mark.parametrize("foreign", [True, False])
async def test_foreign_session_or_wrong_kind_cannot_hydrate_or_execute(tmp_path, erp, foreign):
    _, provider = await host(tmp_path, erp)
    operation = provider.operations.prepare_cancel("foreign" if foreign else "s1", erp[3].order_id)
    app, _ = await host(
        tmp_path, erp, operation, kind="cancel_order" if foreign else "adjust_stock"
    )
    approval = await pending(app)
    state = await app.state.service.session_state("s1")
    assert "operation" not in state["pending_approvals"][0]
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    assert erp[2].get_stock("A").quantity == 990
    assert (
        provider.operations.get("foreign" if foreign else "s1", operation["operation_id"])["status"]
        == "open"
    )


@pytest.mark.parametrize("kind", ["cancel_order", "adjust_stock"])
async def test_committed_response_loss_replays_without_second_mutation(tmp_path, erp, kind):
    _, provider = await host(tmp_path, erp)
    operation = (
        provider.operations.prepare_cancel("s1", erp[3].order_id)
        if kind == "cancel_order"
        else provider.operations.prepare_adjust("s1", "A", -3)
    )
    saver = InMemorySaver()
    app, _ = await host(tmp_path, erp, operation, kind=kind, saver=saver)
    approval = await pending(app)
    provider.operations.execute(
        "s1", operation["operation_id"], kind, approval.arguments["client_token"]
    )
    restored, _ = await host(tmp_path, erp, operation, kind=kind, saver=saver)
    state = await restored.state.service.session_state("s1")
    assert state["pending_approvals"][0]["operation"]["status"] == "executed"
    events = [
        event
        async for event in restored.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await restored.state.service.session_state("s1")
    assert state["tool_results"][0]["ok"] is True
    assert erp[2].get_stock("A").quantity == (1000 if kind == "cancel_order" else 987)


async def test_expired_operation_remains_exact_and_cannot_execute(tmp_path, erp):
    from erp_store.operations import ErpOperations

    earlier = datetime.now(UTC) - timedelta(minutes=31)
    operation = ErpOperations(erp[1], clock=lambda: earlier).prepare_adjust("s1", "A", -3)
    app, _ = await host(tmp_path, erp, operation)
    approval = await pending(app)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/sessions/s1/operations/{operation['operation_id']}")
    assert response.json()["status"] == "expired"
    assert response.json()["summary"] == operation["summary"]
    state = await app.state.service.session_state("s1")
    assert state["pending_approvals"][0]["operation"] == response.json()
    events = [
        event async for event in app.state.service.stream_resume("s1", approval.pending_id, True)
    ]
    assert events[-1].completed
    state = await app.state.service.session_state("s1")
    assert "operation_expired" in state["tool_results"][0]["content"]
    assert erp[2].get_stock("A").quantity == 990
