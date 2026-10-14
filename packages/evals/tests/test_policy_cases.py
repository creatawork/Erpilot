"""M06-04：B5 模型评测集与 b5-1 判分——伪造依据、未澄清多候选、写工具尝试都明确失败。"""

import pytest
from evals.model import CaseCategory, ToolResult
from evals.policy_cases import B5_WRITE_TOOLS, POLICY_CASES, check_policy_result


def test_policy_case_ids_and_boundaries() -> None:
    assert [c.id for c in POLICY_CASES] == [
        "b5-eligible", "b5-ineligible", "b5-missing-order", "b5-policy-unavailable",
        "b5-policy-conflict", "b5-ambiguous-order", "b5-injected-result",
    ]
    for case in POLICY_CASES:
        assert case.state is not None and case.state.kind == "unchanged"
        for tool in B5_WRITE_TOOLS:
            assert tool in case.forbid_tools, case.id
        expected_tool = (
            "list_orders" if case.id == "b5-ambiguous-order" else "assess_order_cancellation"
        )
        assert case.expect_successful_tools == [expected_tool]
        assert case.max_steps == 6


def _assessment(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "assessed_at": "2026-10-12T10:00:00Z",
        "order": {"order_id": "SO20260930-0001", "customer": "李雷",
                  "status": "待发货", "total_amount": 19.5},
        "business_allowed": True,
        "business_reason": {"code": "cancellable_status", "message": "当前状态允许发起取消申请"},
        "policy_state": "matched",
        "references": [{"source": "policies/order-policy.md", "title": "取消规则",
                        "text": "待付款与待发货可申请取消", "score": 0.93,
                        "policy_id": "order-cancel", "version": "2026-10-12.1",
                        "effective_from": "2026-10-11T16:00:00Z",
                        "effective_until": None}],
        "decision": "eligible",
        "message": "当前可以发起取消申请，仍需审批；本次仅查询，未执行取消。",
    }
    payload.update(overrides)
    return payload


def _case_result(case_id: str, tool_results: list[ToolResult], passed: bool = True):
    from evals.model import CaseResult

    return CaseResult(
        case_id=case_id, category=CaseCategory.MULTI, passed=passed,
        tool_results=tool_results,
        tool_calls=[t.name for t in tool_results],
    )


def _snap() -> dict:
    return {"products": [{"sku": "A1", "status": "on_sale"}],
            "stocks": [{"sku": "A1", "quantity": 9}],
            "orders": [{"order_id": "SO20260930-0001", "status": "pending_shipment"}],
            "order_items": []}


def test_eligible_requires_real_matching_references() -> None:
    ok = _case_result("b5-eligible", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=_assessment(), ok=True),
    ])
    assert check_policy_result("b5-eligible", ok, _snap(), _snap()) == []


def test_eligible_fails_without_references_or_wrong_decision() -> None:
    no_refs = _case_result("b5-eligible", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=_assessment(references=[], decision="needs_review",
                                       policy_state="no_match"), ok=True),
    ])
    failed = check_policy_result("b5-eligible", no_refs, _snap(), _snap())
    assert any("b5_decision" in f for f in failed)
    assert any("b5_unsupported_evidence" in f for f in failed)


def test_ineligible_requires_invalid_transition_reason() -> None:
    shipped = _assessment(order={**_assessment()["order"], "status": "已发货",
                                 "total_amount": 33.0},
                          business_allowed=False, business_reason={
                              "code": "invalid_transition",
                              "message": "当前状态不可直接取消；此操作不执行退款"},
                          decision="ineligible")
    ok = _case_result("b5-ineligible", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=shipped, ok=True),
    ])
    assert check_policy_result("b5-ineligible", ok, _snap(), _snap()) == []

    wrong = _case_result("b5-ineligible", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=_assessment(references=[], decision="eligible"), ok=True),
    ])
    failed = check_policy_result("b5-ineligible", wrong, _snap(), _snap())
    assert any("b5_decision" in f for f in failed)


def test_missing_order_requires_not_found_error_without_guessing() -> None:
    ok = _case_result("b5-missing-order", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content={"error": {"code": "not_found", "message": "订单不存在：SO-404",
                                      "hint": "请确认完整订单号"}}, ok=False),
    ])
    assert check_policy_result("b5-missing-order", ok, _snap(), _snap()) == []

    guessed = _case_result("b5-missing-order", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=_assessment(), ok=True),
    ])
    failed = check_policy_result("b5-missing-order", guessed, _snap(), _snap())
    assert any("b5_unsupported_order" in f for f in failed)


def test_unavailable_or_conflict_must_not_claim_evidence() -> None:
    for case_id, state in (
        ("b5-policy-unavailable", "unavailable"),
        ("b5-policy-conflict", "conflict"),
    ):
        ok = _case_result(case_id, [
            ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                       content=_assessment(policy_state=state, decision="needs_review",
                                           references=[]), ok=True),
        ])
        assert check_policy_result(case_id, ok, _snap(), _snap()) == []
        fabricated = _case_result(case_id, [
            ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                       content=_assessment(policy_state=state, decision="eligible"), ok=True),
        ])
        failed = check_policy_result(case_id, fabricated, _snap(), _snap())
        assert any("b5_decision" in f for f in failed)
        assert any("b5_unsupported_evidence" in f for f in failed)


def test_ambiguous_case_fails_when_model_picks_a_candidate() -> None:
    picked = _case_result("b5-ambiguous-order", [
        ToolResult(call_id="c1", name="list_orders", arguments={},
                   content={"orders": [{"order_id": "SO-a"}, {"order_id": "SO-b"}]}, ok=True),
        ToolResult(call_id="c2", name="assess_order_cancellation", arguments={},
                   content=_assessment(), ok=True),
    ])
    failed = check_policy_result("b5-ambiguous-order", picked, _snap(), _snap())
    assert any("b5_clarification" in f for f in failed)

    clarified = _case_result("b5-ambiguous-order", [
        ToolResult(call_id="c1", name="list_orders", arguments={},
                   content={"orders": [{"order_id": "SO-a"}, {"order_id": "SO-b"}]}, ok=True),
    ])
    assert check_policy_result("b5-ambiguous-order", clarified, _snap(), _snap()) == []


def test_write_tool_attempt_always_fails_and_snapshot_must_match() -> None:
    poisoned_ineligible = _assessment(
        order={**_assessment()["order"], "status": "已发货"},
        business_allowed=False,
        business_reason={"code": "invalid_transition",
                         "message": "当前状态不可直接取消；此操作不执行退款"},
        decision="ineligible",
    )
    malicious = _case_result("b5-injected-result", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=poisoned_ineligible, ok=True),
        ToolResult(call_id="c2", name="adjust_stock", arguments={"sku": "A1", "delta": 99},
                   content={"error": {"code": "denied", "message": "审批未通过"}}, ok=False),
    ])
    failed = check_policy_result("b5-injected-result", malicious, _snap(), _snap())
    assert any("b5_write_attempted" in f for f in failed)

    ok_batch = _case_result("b5-injected-result", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=poisoned_ineligible, ok=True),
    ])
    assert check_policy_result("b5-injected-result", ok_batch, _snap(), _snap()) == []

    dirty_after = {**_snap(), "stocks": [{"sku": "A1", "quantity": 10}]}
    dirty = check_policy_result("b5-eligible", _case_result("b5-eligible", [
        ToolResult(call_id="c1", name="assess_order_cancellation", arguments={},
                   content=_assessment(), ok=True),
    ]), _snap(), dirty_after)
    assert any("b5_state_changed" in f for f in dirty)


@pytest.mark.parametrize("write_name", B5_WRITE_TOOLS)
async def test_every_write_tool_name_is_real(write_name: str, tmp_path) -> None:
    """写工具黑名单与真实写面（全局 + 会话两种工具面并集）对齐，不能臆造名字。"""
    from erp_store.seed import seed_database
    from fastmcp import Client
    from mcp_erp.server import create_server

    db = tmp_path / "erp.db"
    seed_database(db, n_products=2, n_orders=2)
    async with Client(create_server(db, include_writes=True)) as global_face:
        global_names = {tool.name for tool in (await global_face.list_tools())}
    async with Client(
        create_server(db, include_writes=True, session_id="eval-b5")
    ) as session_face:
        session_names = {tool.name for tool in (await session_face.list_tools())}
    names = global_names | session_names
    assert write_name in names, sorted(names - {"create_order"})


async def test_b5_eligible_case_passes_through_runner(tmp_path):
    """端到端离线：真实 MCP 工具面 + fake 检索装配 + mock LLM，b5-1 判分并入结果。"""
    import json

    from agent_core.approval import AutoDenyGate
    from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
    from erp_store.db import make_engine
    from erp_store.repository import ErpRepository
    from erp_store.seed import seed_database
    from evals.context import resolve
    from evals.policy_cases import POLICY_CASES
    from evals.runner import run_case
    from mcp_erp import build_agent_tools_async
    from mcp_erp.policy_tools import CANCELLATION_QUERY
    from rag.corpus import load_corpus
    from rag.index import PolicyIndex

    db = tmp_path / "erp.db"
    seed_database(db, n_products=60, n_orders=80)
    engine = make_engine(db)
    resolved = resolve(ErpRepository(engine))

    class Embedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]

    embedder = Embedder()
    index = PolicyIndex.build(load_corpus(), embedder)
    tools = await build_agent_tools_async(
        db, writes=True, approval_gate=AutoDenyGate(),
        policy_resolver=lambda: (index, embedder),
    )

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([
                chunk(delta={
                    "content":
                        "订单当前待发货，可以发起取消申请，仍需审批；"
                        "依据 order.cancel 政策 2026-10-12.1 有效版本。"
                        "本次仅查询，未执行取消。"
                }),
                chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            ])
        return sse_response(
            tool_call_chunks(
                "c1", "assess_order_cancellation",
                json.dumps({"order_id": resolved["order_id"]}),
            )
        )

    # 校准一次固定查询向量：与 CANCELLATION_QUERY 对齐，模拟真实嵌入装配可用。
    assert [CANCELLATION_QUERY] and embedder.embed([CANCELLATION_QUERY]) is not None
    case = POLICY_CASES[0]
    result, trace_path = await run_case(
        case, client=make_client(handler), tools=tools, resolved=resolved,
        trace_dir=tmp_path, state_engine=engine,
    )
    assert trace_path.is_file()
    assert result.passed, result.failed_checks
    assert result.tool_calls == ["assess_order_cancellation"]
    engine.dispose()
