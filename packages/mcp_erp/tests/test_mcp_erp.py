"""mcp_erp 单测：MCP server 工具面、bridge 转换、loop→bridge→真库全链路。

LLM 在 HTTP 边界 mock（agent_core.testing），ERP 数据用小规模确定性种子，
验证的是"agent 循环经 MCP 协议查到真数据"这条完整链路。
"""

import json
from pathlib import Path

import pytest
from agent_core.loop import AgentLoop, ToolCallFinished
from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from mcp_erp import build_agent_tools, build_agent_tools_async, create_server


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    return path


@pytest.fixture(scope="module")
def repo(seeded_db) -> ErpRepository:
    return ErpRepository(make_engine(seeded_db))


# ---- MCP server 工具面 ----


async def test_server_exposes_ten_readonly_tools(seeded_db) -> None:
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == {
        "get_order", "list_orders", "get_product", "search_products",
        "get_stock", "compute_quote", "list_low_stock", "sales_summary",
        "top_products", "list_categories",
    }


async def test_get_order_returns_error_dict_when_missing(seeded_db) -> None:
    """查不到返回结构化错误（给模型看的信息），永不返回 None。"""
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        result = await client.call_tool("get_order", {"order_id": "SO20990101-9999"})
    assert result.data == {"error": "订单不存在：SO20990101-9999"}


async def test_list_orders_payload_has_total_and_filters(seeded_db) -> None:
    from fastmcp import Client

    async with Client(create_server(seeded_db)) as client:
        ok = await client.call_tool("list_orders", {"limit": 5})
        cancelled = await client.call_tool("list_orders", {"status": "已取消"})
        bad = await client.call_tool("list_orders", {"status": "不存在状态"})
    assert len(ok.data["items"]) == 5 and ok.data["total"] >= 5
    assert cancelled.data["total"] > 0
    assert all(o["status"] == "已取消" for o in cancelled.data["items"])
    assert "无效的订单状态" in bad.data["error"]


# ---- bridge：MCP 工具 → agent_core Tool ----


def test_bridge_builds_agent_tools(seeded_db) -> None:
    tools = build_agent_tools(seeded_db)

    assert len(tools) == 10
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
