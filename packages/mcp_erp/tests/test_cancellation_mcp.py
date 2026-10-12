import sqlite3

import pytest
from agent_core.approval import AutoDenyGate
from erp_store.db import ProductRow, StockRow, init_db, make_engine
from erp_store.mutations import ErpMutations
from fastmcp import Client
from mcp_erp.bridge import WRITE_TOOL_RISK, build_agent_tools_async
from mcp_erp.server import create_server
from rag.corpus import load_corpus
from rag.index import PolicyIndex
from sqlalchemy.orm import Session


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "erp.db"
    engine = make_engine(path)
    init_db(engine)
    with Session(engine) as session:
        session.add(ProductRow(sku="A", name="商品甲", category="测试", price=9.75, status="在售"))
        session.add(StockRow(sku="A", quantity=100, warehouse="主仓"))
        session.commit()
    order = ErpMutations(engine).create_order("甲", [("A", 2)])

    class Embedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]

    embedder = Embedder()
    index = PolicyIndex.build(load_corpus(), embedder)
    return path, order, lambda: (index, embedder)


def dump(path):
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


@pytest.mark.parametrize("writes", [False, True])
@pytest.mark.parametrize("scope", [None, "s1"])
async def test_assessment_has_no_database_side_effects(setup, writes, scope):
    path, order, resolver = setup
    before = dump(path)
    async with Client(
        create_server(path, include_writes=writes, session_id=scope, policy_resolver=resolver)
    ) as client:
        schemas = {t.name: t.input_schema for t in await client.list_tools()}
        assert "assess_order_cancellation" in schemas
        assert set(schemas["assess_order_cancellation"]["properties"]) == {"order_id"}
        result = (
            await client.call_tool(
                "assess_order_cancellation", {"order_id": " " + order.order_id + " "}
            )
        ).data
    assert result["decision"] == "eligible"
    assert result["order"]["total_amount"] == order.total_amount == 19.5
    assert result["order"]["customer"] == "甲"
    assert dump(path) == before


async def test_bad_order_returns_before_resolver(setup):
    path, _, _ = setup

    def forbidden():
        raise AssertionError("must not resolve policy")

    async with Client(create_server(path, policy_resolver=forbidden)) as client:
        for order_id, code in [(" ", "invalid_argument"), ("SO-MISSING", "not_found")]:
            result = (
                await client.call_tool("assess_order_cancellation", {"order_id": order_id})
            ).data
            assert result["error"]["code"] == code


async def test_policy_unavailable_preserves_order_facts(setup):
    path, order, _ = setup

    def unavailable():
        raise RuntimeError("secret-credential")

    before = dump(path)
    tools = await build_agent_tools_async(
        path,
        writes=True,
        approval_gate=AutoDenyGate(),
        session_id="s1",
        policy_resolver=unavailable,
    )
    tool = next(t for t in tools if t.name == "assess_order_cancellation")
    assert tool.risk is None and tool.name not in WRITE_TOOL_RISK
    result = await tool.handler(tool.params_model(order_id=order.order_id))
    assert result["policy_state"] == "unavailable"
    assert result["decision"] == "needs_review"
    assert result["order"]["total_amount"] == 19.5
    assert "secret" not in str(result)
    assert dump(path) == before
