import json
import sqlite3

import httpx2
import pytest
from agent_core.approval import StreamApprovalGate
from agent_core.events import ApprovalPending
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erp_store.db import OrderRow, ProductRow, StockRow, init_db, make_engine
from erp_store.mutations import ErpMutations, MutationError
from erp_store.repository import ErpRepository
from erpilot_api.main import create_app
from langgraph.checkpoint.memory import InMemorySaver
from mcp_erp.policy_tools import CANCELLATION_GUIDANCE
from mcp_erp.session_tools import SessionErpTools
from rag.corpus import load_corpus
from rag.index import PolicyIndex
from sqlalchemy.orm import Session


def dump(path):
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


@pytest.fixture
def erp(tmp_path):
    path = tmp_path / "erp.db"
    engine = make_engine(path)
    init_db(engine)
    with Session(engine) as session:
        session.add(ProductRow(sku="A", name="商品", category="测试", price=10, status="在售"))
        session.add(StockRow(sku="A", quantity=100, warehouse="主仓"))
        session.commit()
    order = ErpMutations(engine).create_order("甲", [("A", 2)])
    return path, engine, order


async def host(tmp_path, erp, *, saver=None, resolver=None, model=None):
    class Embedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]

    embedder = Embedder()
    index = PolicyIndex.build(load_corpus(), embedder)
    provider = await SessionErpTools.create(
        erp[0],
        writes=True,
        gate=StreamApprovalGate(),
        policy_resolver=resolver or (lambda: (index, embedder)),
    )

    def scripted(request):
        messages = json.loads(request.content)["messages"]
        assert CANCELLATION_GUIDANCE in messages[0]["content"]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if messages[-1]["role"] == "tool":
            payload = json.loads(messages[-1]["content"])
            if messages[-1]["tool_call_id"] == "prepare":
                return sse_response(
                    tool_call_chunks(
                        "cancel",
                        "cancel_order",
                        json.dumps({"operation_id": payload["operation_id"]}),
                    )
                )
            return sse_response(
                [chunk(delta={"content": "本次仅查询，未执行取消。"}), chunk(usage=USAGE)]
            )
        tool = "prepare_cancel_order" if user == "明确取消" else "assess_order_cancellation"
        return sse_response(
            tool_call_chunks(
                "assessment" if tool.startswith("assess") else "prepare",
                tool,
                json.dumps({"order_id": erp[2].order_id}),
            )
        )

    app = create_app(
        client_factory=lambda: make_client(model or scripted),
        tools=provider.tools,
        session_erp_tools=provider,
        trace_dir=tmp_path,
        checkpointer=saver or InMemorySaver(),
    )
    return app, provider


async def test_query_restore_then_explicit_cancel_requires_approval(tmp_path, erp):
    saver = InMemorySaver()
    app, _ = await host(tmp_path, erp, saver=saver)
    before = dump(erp[0])
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat/stream", json={"session_id": "s1", "message": "还能取消吗"}
        )
        assert response.status_code == 200 and "event: done" in response.text
        assert "event: approval_pending" not in response.text
        state = (await client.get("/api/sessions/s1/state")).json()
    assert state["pending_approvals"] == []
    projected = state["tool_results"][0]["display"]
    assert projected["assessment"]["decision"] == "eligible"
    assert dump(erp[0]) == before
    # Simulate an obsolete stored system message, then rebuild the host over the same saver.
    runtime = app.state.service._runtime("s1")
    from erpilot_api.service import thread_id_for_session

    snapshot = await runtime.get_state(thread_id_for_session("s1"))
    history = snapshot.values["messages"]
    history[0]["content"] = "obsolete system prompt"
    await runtime.graph.aupdate_state(
        {"configurable": {"thread_id": thread_id_for_session("s1")}}, {"messages": history}
    )
    calls = []

    def unavailable():
        calls.append(True)
        raise RuntimeError("must not requery history")

    restored, _ = await host(tmp_path, erp, saver=saver, resolver=unavailable)
    recovered = await restored.state.service.session_state("s1")
    assert recovered["tool_results"][0]["display"] == projected
    assert calls == [] and dump(erp[0]) == before
    stream = restored.state.service.stream_run("s1", "明确取消")
    async for event in stream:
        if isinstance(event, ApprovalPending):
            approval = event
            await stream.aclose()
            break
    else:
        pytest.fail("expected explicit cancellation approval")
    assert approval.tool == "cancel_order"
    assert "operation_id" in approval.arguments
    assert calls == []
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(restored), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat/approve/stream",
            json={"session_id": "s1", "pending_id": approval.pending_id, "approved": False},
        )
        assert response.status_code == 200 and "event: done" in response.text
    repo = ErpRepository(erp[1])
    assert repo.get_order(erp[2].order_id).status.value == "待付款"
    assert repo.get_stock("A").quantity == 98


async def test_prior_query_does_not_authorize_changed_order(tmp_path, erp):
    app, provider = await host(tmp_path, erp)
    events = [e async for e in app.state.service.stream_run("s1", "还能取消吗")]
    assert not any(isinstance(e, ApprovalPending) for e in events)
    with Session(erp[1]) as session:
        session.get(OrderRow, erp[2].order_id).status = "已发货"
        session.commit()
    with pytest.raises(MutationError, match="取消"):
        provider.operations.prepare_cancel("s1", erp[2].order_id)
    assert ErpRepository(erp[1]).get_stock("A").quantity == 98


async def test_ambiguous_script_clarifies_without_writing(tmp_path, erp):
    ErpMutations(erp[1]).create_order("甲", [("A", 1)])

    def model(request):
        messages = json.loads(request.content)["messages"]
        assert CANCELLATION_GUIDANCE in messages[0]["content"]
        if messages[-1]["role"] != "tool":
            return sse_response(tool_call_chunks("list", "list_orders", "{}"))
        assert json.loads(messages[-1]["content"])["total"] == 2
        return sse_response(
            [chunk(delta={"content": "请确认完整订单号，不能猜测最新一单。"}), chunk(usage=USAGE)]
        )

    app, _ = await host(tmp_path, erp, model=model)
    before = dump(erp[0])
    events = [e async for e in app.state.service.stream_run("s1", "帮我看看那张单")]
    assert not any(isinstance(e, ApprovalPending) for e in events)
    assert dump(erp[0]) == before
