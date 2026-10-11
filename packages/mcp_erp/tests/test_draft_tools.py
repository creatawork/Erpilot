import json

import pytest
from agent_core.approval import StreamApprovalGate
from erp_store.db import ProductRow, StockRow, init_db, make_engine
from erp_store.models import ProductStatus
from fastmcp import Client
from mcp_erp.server import create_server
from mcp_erp.session_tools import SessionErpTools
from sqlalchemy.orm import Session


@pytest.fixture
def db(tmp_path):
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
    return path


async def test_scoped_mcp_tools_hide_raw_create_and_session_parameter(db):
    async with Client(create_server(db, include_writes=True, session_id="s1")) as client:
        schemas = {tool.name: tool.input_schema for tool in await client.list_tools()}
        assert "create_order" not in schemas
        assert "submit_order_draft" in schemas and "prepare_order_draft" in schemas
        assert all("session_id" not in schema["properties"] for schema in schemas.values())
        prepared = await client.call_tool(
            "prepare_order_draft",
            {
                "customer": "客户",
                "items": [{"sku": "A", "quantity": 10}],
            },
        )
    draft = prepared.data
    assert draft["total_amount"] == 980.0
    async with Client(create_server(db, include_writes=True, session_id="s2")) as client:
        stolen = await client.call_tool("get_order_draft", {"draft_id": draft["draft_id"]})
        submitted = await client.call_tool(
            "submit_order_draft",
            {
                "draft_id": draft["draft_id"],
                "revision": 1,
                "client_token": "forged",
            },
        )
    assert stolen.data["error"]["code"] == submitted.data["error"]["code"] == "not_found"


async def test_session_factory_rebinds_handlers_and_reconciliation(db):
    factory = await SessionErpTools.create(db, writes=True, gate=StreamApprovalGate())
    drafts = factory.drafts
    draft = drafts.prepare("s1", "客户", [("A", 10)])
    args = {"draft_id": draft["draft_id"], "revision": 1, "client_token": "approved"}
    own = {tool.name: tool for tool in factory.for_session("s1")}["submit_order_draft"]
    other = {tool.name: tool for tool in factory.for_session("s2")}["submit_order_draft"]
    assert own.risk == "single_confirm" and own.retry_safe
    # Emulate the original handler that the graph invokes only after its durable approval.
    order = await own.approved_handler(own.params_model.model_validate(args))
    assert order["total_amount"] == 980.0
    stolen = await other.approved_handler(other.params_model.model_validate(args))
    assert stolen["error"]["code"] == "not_found"
    lookup = await factory.reconciler("s1").lookup("submit_order_draft", args, "approved")
    assert lookup.status == "found" and lookup.result["order_id"] == order["order_id"]
    other_lookup = await factory.reconciler("s2").lookup("submit_order_draft", args, "approved")
    assert other_lookup.status == "absent"
    assert json.dumps(own.openai_schema()).find("session_id") == -1


async def test_readonly_session_can_edit_drafts_but_cannot_submit(db):
    factory = await SessionErpTools.create(db, writes=False)
    names = {tool.name for tool in factory.for_session("readonly")}
    assert {"prepare_order_draft", "get_order_draft", "revise_order_draft"} <= names
    assert "submit_order_draft" not in names and "create_order" not in names


async def test_task_tools_rebuild_context_and_edit_without_business_write(db):
    async with Client(create_server(db, session_id="s1")) as client:
        schemas = {tool.name: tool.input_schema for tool in await client.list_tools()}
        assert "get_order_task" in schemas and "revise_order_draft_quantity" in schemas
        assert schemas["get_order_task"]["properties"] == {}
        assert set(schemas["revise_order_draft_quantity"]["properties"]) == {
            "draft_id", "expected_revision", "item_number", "quantity"
        }
        assert (await client.call_tool("get_order_task", {})).data["status"] == "none"
        draft = (await client.call_tool("prepare_order_draft", {
            "customer": "保留客户", "items": [{"sku": "A", "quantity": 2}], "note": "保留备注"
        })).data
    async with Client(create_server(db, session_id="s1")) as client:
        state = (await client.call_tool("get_order_task", {})).data
        assert state["status"] == "active" and state["draft"]["draft_id"] == draft["draft_id"]
        arguments = {
            "draft_id": draft["draft_id"], "expected_revision": 1, "item_number": 1, "quantity": 50
        }
        changed = (await client.call_tool("revise_order_draft_quantity", arguments)).data
        assert changed["customer"] == "保留客户" and changed["note"] == "保留备注"
        assert changed["items"][0]["quantity"] == 50 and changed["revision"] == 2
        assert changed["order_id"] is None
        stale = (await client.call_tool("revise_order_draft_quantity", arguments)).data
        assert stale["error"]["code"] == "draft_changed"
    async with Client(create_server(db, session_id="other")) as client:
        assert (await client.call_tool("get_order_task", {})).data["status"] == "none"
        stolen = (await client.call_tool("revise_order_draft_quantity", {
            **arguments, "expected_revision": 2
        })).data
        assert stolen["error"]["code"] == "not_found"
    factory = await SessionErpTools.create(db, writes=False)
    tools = {tool.name: tool for tool in factory.for_session("s1")}
    assert tools["get_order_task"].risk == tools["get_order_draft"].risk
    assert tools["revise_order_draft_quantity"].risk == tools["revise_order_draft"].risk
    assert "submit_order_draft" not in tools


@pytest.mark.parametrize("key,value", [
    ("expected_revision", True), ("expected_revision", "1"),
    ("item_number", True), ("item_number", 1.5), ("quantity", True), ("quantity", "3"),
])
async def test_quantity_tool_rejects_coerced_numbers(db, key, value):
    async with Client(create_server(db, session_id="s1")) as client:
        draft = (await client.call_tool("prepare_order_draft", {
            "customer": "客户", "items": [{"sku": "A", "quantity": 2}]
        })).data
        args = {
            "draft_id": draft["draft_id"], "expected_revision": 1, "item_number": 1, "quantity": 3
        }
        result = await client.call_tool(
            "revise_order_draft_quantity", {**args, key: value}, raise_on_error=False
        )
        assert result.is_error
        current = (await client.call_tool("get_order_draft", {
            "draft_id": draft["draft_id"]
        })).data
        assert current["revision"] == 1
