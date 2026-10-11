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
    with Session(engine) as s:
        s.add(
            ProductRow(
                sku="A", name="商品", category="测试", price=100, status=ProductStatus.ON_SALE.value
            )
        )
        s.add(StockRow(sku="A", quantity=100, warehouse="主仓"))
        s.commit()
    return path


async def test_scoped_execution_uses_reference_and_legacy_keeps_raw_schema(db):
    async with Client(create_server(db, include_writes=True, session_id="s1")) as client:
        tools = {t.name: t.input_schema for t in await client.list_tools()}
        assert set(tools["cancel_order"]["properties"]) == {"operation_id", "client_token"}
        assert set(tools["adjust_stock"]["properties"]) == {"operation_id", "client_token"}
        assert "prepare_cancel_order" in tools and "prepare_stock_adjustment" in tools
        assert all("session_id" not in t["properties"] for t in tools.values())
        p = (await client.call_tool("prepare_stock_adjustment", {"sku": "A", "delta": -3})).data
        result = (
            await client.call_tool(
                "adjust_stock", {"operation_id": p["operation_id"], "client_token": "first"}
            )
        ).data
        assert result["quantity"] == 97 and result["delta"] == -3
    async with Client(create_server(db, include_writes=True)) as client:
        tools = {t.name: t.input_schema for t in await client.list_tools()}
        assert "order_id" in tools["cancel_order"]["properties"]
        assert "sku" in tools["adjust_stock"]["properties"]


async def test_factory_recovery_cannot_leak_other_sessions(db):
    provider = await SessionErpTools.create(db, writes=True, gate=StreamApprovalGate())
    p = provider.operations.prepare_adjust("s1", "A", 5)
    tool = {t.name: t for t in provider.for_session("s1")}["adjust_stock"]
    assert tool.risk == "batch_confirm" and tool.retry_safe
    args = {"operation_id": p["operation_id"], "client_token": "approval"}
    result = await tool.approved_handler(tool.params_model.model_validate(args))
    assert result["quantity"] == 105
    lookup = await provider.reconciler("s1").lookup("adjust_stock", args, "approval")
    assert lookup.status == "found" and lookup.result == result
    assert (
        await provider.reconciler("s2").lookup("adjust_stock", args, "approval")
    ).status == "absent"
    stolen = {t.name: t for t in provider.for_session("s2")}["adjust_stock"]
    assert (await stolen.approved_handler(stolen.params_model.model_validate(args)))["error"][
        "code"
    ] == "not_found"


async def test_readonly_mode_has_previews_but_no_execution(db):
    p = await SessionErpTools.create(db, writes=False)
    names = {t.name for t in p.for_session("s1")}
    assert {"prepare_cancel_order", "prepare_stock_adjustment"} <= names
    assert not {"cancel_order", "adjust_stock"} & names
