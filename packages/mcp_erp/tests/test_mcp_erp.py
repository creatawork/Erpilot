"""mcp_erp 单测：MCP server 工具面、bridge 转换、loop→bridge→真库全链路。

LLM 在 HTTP 边界 mock（agent_core.testing），ERP 数据用小规模确定性种子，
验证的是"agent 循环经 MCP 协议查到真数据"这条完整链路。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from agent_core.loop import AgentLoop, ToolCallFinished
from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import OrderItemRow, OrderRow, init_db, make_engine
from erp_store.models import OrderStatus
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from mcp_erp import build_agent_tools, build_agent_tools_async, create_server
from sqlalchemy.orm import Session


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    return path


@pytest.fixture(scope="module")
def repo(seeded_db) -> ErpRepository:
    return ErpRepository(make_engine(seeded_db))


# ---- MCP server 工具面 ----

EXPECTED_TOOLS = {
    "get_order", "list_orders", "get_orders_by_sku", "get_customer_purchases",
    "get_product", "search_products", "list_products",
    "get_stock", "compute_quote", "compare_quotes", "list_low_stock",
    "sales_summary", "top_products", "daily_sales", "stock_valuation",
    "list_categories",
}


async def test_server_exposes_all_readonly_tools(seeded_db) -> None:
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 16  # §4 冻结线 15~20 区间内


async def test_get_order_error_contract_v1(seeded_db) -> None:
    """查不到返回 {"error": {code, message, hint}}——hint 指出下一步。"""
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        result = await client.call_tool("get_order", {"order_id": "SO20990101-9999"})
    err = result.data["error"]
    assert err["code"] == "not_found"
    assert "SO20990101-9999" in err["message"]
    assert "list_orders" in err["hint"]


async def test_list_orders_payload_has_total_and_filters(seeded_db) -> None:
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        ok = await client.call_tool("list_orders", {"limit": 5})
        cancelled = await client.call_tool("list_orders", {"status": "已取消"})
        bad = await client.call_tool("list_orders", {"status": "不存在状态"})
    assert len(ok.data["items"]) == 5 and ok.data["total"] >= 5
    assert cancelled.data["total"] > 0
    assert all(o["status"] == "已取消" for o in cancelled.data["items"])
    assert bad.data["error"]["code"] == "invalid_argument"
    assert "可选" in bad.data["error"]["hint"]


async def test_list_orders_slim_by_default(seeded_db) -> None:
    """列表默认订单头摘要（明细是上下文的大头），detail=True 才带明细。"""
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        slim = await client.call_tool("list_orders", {"limit": 5})
        detail = await client.call_tool("list_orders", {"limit": 5, "detail": True})
    first = slim.data["items"][0]
    assert "items" not in first and first["items_count"] >= 0
    assert "get_customer_purchases" in slim.data["note"]
    assert "items" in detail.data["items"][0]


async def test_get_customer_purchases_aggregates(seeded_db, repo) -> None:
    from fastmcp import Client

    customer = repo.list_orders(limit=1)[0].customer
    async with Client(create_server(seeded_db)) as client:
        ok = await client.call_tool("get_customer_purchases", {"customer": customer})
        missing = await client.call_tool(
            "get_customer_purchases", {"customer": "不存在的客户xyz"}
        )
    assert ok.data["order_count"] == repo.count_orders(customer=customer)
    assert ok.data["by_status"] and ok.data["items"]
    assert missing.data["error"]["code"] == "not_found"


async def test_list_orders_detail_clamps_page_size(seeded_db) -> None:
    """detail=True 收紧页大小到 20；note 如实说明带没带明细（不说反话）。"""
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        clamped = await client.call_tool("list_orders", {"limit": 50, "detail": True})
        as_asked = await client.call_tool("list_orders", {"limit": 5, "detail": True})
        slim = await client.call_tool("list_orders", {"limit": 50})
    assert len(clamped.data["items"]) == 20
    assert "已带每单明细" in clamped.data["note"] and "收紧到 20" in clamped.data["note"]
    assert len(as_asked.data["items"]) == 5
    assert "收紧" not in as_asked.data["note"]
    assert "列表不含明细" in slim.data["note"]


async def test_get_customer_purchases_truncates_items_at_40(tmp_path) -> None:
    """聚合行 > 40 时服务端截前 40 并给 note；仓库层仍是全量（截断是表现层策略）。"""
    from fastmcp import Client

    db = tmp_path / "big.db"
    engine = make_engine(db)
    init_db(engine)
    with Session(engine) as session:
        for j in range(3):  # 45 个不同 SKU 分三单，全部归到独立客户名下
            session.add(OrderRow(
                order_id=f"SO20260101-{j + 1:04d}",
                customer="测试大户",
                status=OrderStatus.PENDING_SHIPMENT.value,
                created_at=datetime(2026, 1, 1) + timedelta(days=j),
                items=[
                    OrderItemRow(sku=f"X{i:03d}", name=f"测试品{i:03d}",
                                 quantity=1, unit_price=10.0 + i)
                    for i in range(j * 15, (j + 1) * 15)
                ],
            ))
        session.commit()

    async with Client(create_server(db)) as client:
        result = await client.call_tool("get_customer_purchases", {"customer": "测试大户"})
    assert result.data["order_count"] == 3
    assert len(result.data["items"]) == 40
    assert result.data["note"] == "仅展示金额最高的 40 种商品"
    full = ErpRepository(engine).customer_purchases("测试大户")
    assert full is not None and len(full.items) == 45
    # 截断保留的是金额最高的一批：第 40 行金额仍高于第 41 名
    assert result.data["items"][-1]["total_amount"] > full.items[40].total_amount


# ---- bridge：MCP 工具 → agent_core Tool ----


def test_bridge_builds_agent_tools(seeded_db) -> None:
    tools = build_agent_tools(seeded_db)

    assert len(tools) == len(EXPECTED_TOOLS)
    by_name = {t.name: t for t in tools}
    # 描述来自工具 docstring（模型选择工具的依据）
    assert "订单" in by_name["get_order"].description
    # inputSchema → Pydantic 模型：required 必填、可选有默认
    schema = by_name["get_order"].openai_schema()
    assert schema["function"]["parameters"]["required"] == ["order_id"]
    assert by_name["list_orders"].params_model.model_fields["limit"].default == 20


def test_bridge_handler_calls_through_mcp(seeded_db, repo) -> None:
    tools = {t.name: t for t in build_agent_tools(seeded_db)}
    order_id = repo.list_orders(limit=1)[0].order_id

    args = tools["get_order"].params_model.model_validate_json(
        json.dumps({"order_id": order_id})
    )
    result = _run(tools["get_order"].handler(args))

    assert result["order_id"] == order_id
    assert result["items"]
    assert result["total_amount"] == round(
        sum(i["quantity"] * i["unit_price"] for i in result["items"]), 2
    )


def test_bridge_handles_array_params_and_error_contract(seeded_db, repo) -> None:
    """compare_quotes 的 list[str] 参数走 schema→create_model→MCP 全程；
    不可报价的 SKU 进 unavailable 而不是报错。"""
    tools = {t.name: t for t in build_agent_tools(seeded_db)}
    skus = [p.sku for p in repo.search_products("茶具", limit=2)]
    off_sale = _first_off_sale_sku(repo)
    args = tools["compare_quotes"].params_model.model_validate_json(
        json.dumps({"skus": [*skus, off_sale], "quantity": 10})
    )
    result = _run(tools["compare_quotes"].handler(args))

    assert result["quantity"] == 10
    assert {q["sku"] for q in result["quotes"]} == set(skus)  # 不可报价的进 unavailable
    totals = [q["total"] for q in result["quotes"]]
    assert totals == sorted(totals)  # 按总价升序
    assert result["unavailable"] == [off_sale]


# ---- 写工具面（M4 第 1 周，ADR-0005） ----

WRITE_TOOLS = {"create_order", "cancel_order", "adjust_stock", "set_product_status"}


async def test_default_server_excludes_write_tools(seeded_db) -> None:
    """写工具默认不存在——工具面收窄是默认态（ADR-0005 决策 4）。"""
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS
    assert not (tools & WRITE_TOOLS)


async def test_write_server_exposes_full_surface(seeded_db) -> None:
    from fastmcp import Client

    async with Client(create_server(seeded_db, include_writes=True)) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS | WRITE_TOOLS
    assert len(tools) == 20  # §4 冻结线 15~20 区间内


async def test_write_error_contract_via_mcp(seeded_db, repo) -> None:
    """mutations 的 MutationError 在工具层转成错误契约 v1（code/message/hint）。"""
    from fastmcp import Client

    delivered = repo.list_orders(status=OrderStatus.DELIVERED, limit=1)[0]
    async with Client(create_server(seeded_db, include_writes=True)) as client:
        bad = await client.call_tool(
            "cancel_order", {"order_id": delivered.order_id}
        )
        missing = await client.call_tool(
            "create_order", {"customer": "张三", "items": [{"sku": "ZZZ999", "quantity": 1}]}
        )
    assert bad.data["error"]["code"] == "invalid_transition"
    assert "已签收" in bad.data["error"]["message"]
    assert bad.data["error"]["hint"]
    assert missing.data["error"]["code"] == "not_found"


def test_bridge_writes_without_gate_raises(seeded_db) -> None:
    """无 gate 不给写工具——结构性保证（ADR-0005 决策 4），不是调用约定。"""
    with pytest.raises(ValueError, match="approval_gate"):
        build_agent_tools(seeded_db, writes=True)


def test_bridge_writes_with_gate_marks_risk_and_guards(seeded_db, repo) -> None:
    """writes=True + gate：写工具带风险等级并包门，只读工具原样。"""
    from agent_core.approval import RISK_BATCH_CONFIRM, RISK_SINGLE_CONFIRM, AutoDenyGate

    tools = {
        t.name: t
        for t in build_agent_tools(seeded_db, writes=True, approval_gate=AutoDenyGate())
    }
    assert set(tools) == EXPECTED_TOOLS | WRITE_TOOLS
    assert tools["create_order"].risk == RISK_SINGLE_CONFIRM
    assert tools["adjust_stock"].risk == RISK_BATCH_CONFIRM
    assert all(tools[name].risk is None for name in EXPECTED_TOOLS)

    # 拒绝路径：handler 返回"未执行"结构，且底层数据未变
    delivered = repo.list_orders(status=OrderStatus.DELIVERED, limit=1)[0]
    args = tools["cancel_order"].params_model.model_validate_json(
        json.dumps({"order_id": delivered.order_id})
    )
    content = _run(tools["cancel_order"].handler(args))
    payload = json.loads(content)
    assert payload["approval"] == "denied"
    assert payload["message"].startswith("操作未执行")


def test_write_tool_risk_map_covers_surface() -> None:
    from mcp_erp.bridge import WRITE_TOOL_RISK

    assert set(WRITE_TOOL_RISK) == WRITE_TOOLS


def test_error_contract_survives_bridge(seeded_db) -> None:
    """业务错误经桥回到 agent 侧仍是 {"error": {code, message, hint}} 结构。"""
    tools = {t.name: t for t in build_agent_tools(seeded_db)}
    args = tools["compute_quote"].params_model.model_validate_json(
        json.dumps({"sku": "NO-SUCH-SKU", "quantity": 1})
    )
    result = _run(tools["compute_quote"].handler(args))
    assert result["error"]["code"] == "not_found"


def _first_off_sale_sku(repo: ErpRepository) -> str:
    for category in ["茶具", "文房", "香道", "瓷器", "丝绸"]:
        for p in repo.search_products(category, limit=100):
            if p.status == "已下架":
                return p.sku
    raise AssertionError("种子数据中没有已下架商品")


def test_tool_cards_doc_matches_server_tools() -> None:
    """工具卡文档与 server 工具集同步——文档过期的唯一方式是先改代码再跑测试。"""
    doc = _REPO_ROOT / "docs" / "tool-cards.md"
    assert doc.is_file(), "docs/tool-cards.md 不存在"
    documented = {
        line.removeprefix("### ").strip()
        for line in doc.read_text(encoding="utf-8").splitlines()
        if line.startswith("### ")
    }
    assert documented == EXPECTED_TOOLS | WRITE_TOOLS


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ---- 全链路：agent loop → bridge → 真库 ----


async def test_agent_loop_queries_real_data_over_mcp(seeded_db, repo) -> None:
    """mock LLM 发起工具调用，走完 loop → MCP → SQLite 的真实链路。"""
    order = repo.list_orders(limit=1)[0]
    requests: list[dict] = []

    def handler(request) -> object:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([
                chunk(delta={"content": f"已查到订单 {order.order_id}"}),
                chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            ])
        return sse_response(
            tool_call_chunks("call_1", "get_order", json.dumps({"order_id": order.order_id}))
        )

    agent = AgentLoop(make_client(handler), tools=await build_agent_tools_async(seeded_db))
    events = [e async for e in agent.run([{"role": "user", "content": f"查订单 {order.order_id}"}])]

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and finished[0].ok is True
    assert json.loads(finished[0].content)["total_amount"] == order.total_amount
    assert events[-1].completed is True
    # 回填进历史的工具结果是真库数据
    tool_msg = next(m for m in requests[1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["customer"] == order.customer
