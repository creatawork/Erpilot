import json

import httpx2
import pytest
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import ProductRow, StockRow, init_db, make_engine
from erp_store.repository import ErpRepository
from erpilot_api.main import create_app
from erpilot_api.run_store import RunStore
from langgraph.checkpoint.memory import InMemorySaver
from mcp_erp.session_tools import SessionErpTools
from sqlalchemy.orm import Session


@pytest.fixture
def erp_path(tmp_path):
    path = tmp_path / "erp.db"
    engine = make_engine(path)
    init_db(engine)
    with Session(engine) as session:
        for sku in ("A", "B"):
            session.add(ProductRow(sku=sku, name=sku, category="测试", price=100, status="在售"))
            session.add(StockRow(sku=sku, quantity=1000, warehouse="主仓"))
        session.commit()
    return path


async def test_order_task_http_none_active_ambiguous_and_isolation(tmp_path, erp_path):
    provider = await SessionErpTools.create(erp_path)
    app = create_app(session_erp_tools=provider, trace_dir=tmp_path, checkpointer=InMemorySaver())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        endpoint = "/api/sessions/s1/order-task"
        assert (await client.get(endpoint)).json()["status"] == "none"
        draft = provider.drafts.prepare("s1", "客户", [("A", 3), ("B", 4)], "保留备注")
        task = (await client.get(endpoint)).json()
        assert task["status"] == "active" and task["draft"] == draft
        assert (await client.get("/api/sessions/s2/order-task")).json()["draft"] is None
        provider.drafts.prepare("s1", "另一客户", [("A", 1)])
        task = (await client.get(endpoint)).json()
        assert task["status"] == "ambiguous" and task["draft"] is None
        assert len(task["candidates"]) == 2


async def test_task_disabled_has_structured_error(tmp_path):
    app = create_app(tools=[], trace_dir=tmp_path, checkpointer=InMemorySaver())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/sessions/s1/order-task")
        assert response.status_code == 503
        assert set(response.json()["error"]) == {"code", "message", "hint"}


async def test_multiround_task_query_then_ordinal_update_survives_new_host(tmp_path, erp_path):
    provider = await SessionErpTools.create(erp_path)
    draft = provider.drafts.prepare("s1", "客户", [("A", 3), ("B", 4)], "保留备注")

    def scripted(request):
        messages = json.loads(request.content)["messages"]
        assert "get_order_task" in messages[0]["content"]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        last = messages[-1]
        if last["role"] != "tool":
            return sse_response(tool_call_chunks("task", "get_order_task", "{}"))
        result = json.loads(last["content"])
        if user == "把第二个改成50件" and result.get("status") == "active":
            current = result["draft"]
            return sse_response(
                tool_call_chunks(
                    "change",
                    "revise_order_draft_quantity",
                    json.dumps(
                        {
                            "draft_id": current["draft_id"],
                            "expected_revision": current["revision"],
                            "item_number": 2,
                            "quantity": 50,
                        }
                    ),
                )
            )
        return sse_response(
            [chunk(delta={"content": "已查询或修改草稿，未建单"}), chunk(usage=USAGE)]
        )

    def host(current):
        return create_app(
            client_factory=lambda: make_client(scripted),
            session_erp_tools=current,
            trace_dir=tmp_path,
            checkpointer=InMemorySaver(),
        )

    app = host(provider)
    assert [event async for event in app.state.service.stream_run("s1", "查草稿")][-1].completed
    store = RunStore(tmp_path / "runs.db")
    saved = store.get_session("s1")["history"]
    saved[0]["content"] = "旧版本系统提示"
    store.update_session_history("s1", saved)
    restored = host(await SessionErpTools.create(erp_path))
    assert [event async for event in restored.state.service.stream_run("s1", "把第二个改成50件")][
        -1
    ].completed
    state = await restored.state.service.session_state("s1")
    current = state["order_task"]["draft"]
    assert current["draft_id"] == draft["draft_id"] and current["revision"] == 2
    assert current["customer"] == "客户" and current["note"] == "保留备注"
    assert [(line["sku"], line["quantity"]) for line in current["items"]] == [("A", 3), ("B", 50)]
    repo = ErpRepository(make_engine(erp_path))
    assert repo.count_orders(customer="客户") == 0 and repo.get_stock("B").quantity == 1000
    assert state["pending_approvals"] == []
