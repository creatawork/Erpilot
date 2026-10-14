"""runner 离线单测：mock LLM 走 run_case / run_all / Budget，不烧 token。"""

import json
from pathlib import Path

from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
from evals.model import CaseCategory, EvalCase
from evals.runner import Budget, run_all, run_case


def _case(**kwargs) -> EvalCase:
    defaults = dict(id="t-01", category=CaseCategory.SINGLE, question="q", points="p")
    return EvalCase(**{**defaults, **kwargs})


def _mock_client(order_id: str):
    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response(
                [
                    chunk(delta={"content": f"订单 {order_id} 状态：待发货"}),
                    chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                ]
            )
        return sse_response(
            tool_call_chunks("call_1", "get_order", json.dumps({"order_id": order_id}))
        )

    return make_client(handler)


async def test_run_case_passes_on_tool_and_text(resolved, tools, tmp_path: Path) -> None:
    case = _case(
        question="查一下订单 {order_id} 的状态",
        expect_tools_any=["get_order"],
        must_mention=["待发货"],
    )
    client = _mock_client(resolved["order_id"])

    result, trace_path = await run_case(
        case, client=client, tools=tools, resolved=resolved, trace_dir=tmp_path
    )
    assert trace_path.is_file()
    assert result.passed, result.failed_checks
    assert result.tool_calls == ["get_order"]
    assert result.completed and result.steps >= 1
    assert result.total_tokens > 0
    assert result.cost is not None and result.cost > 0
    assert result.error is None


async def test_each_eval_case_gets_an_isolated_graph_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    import evals.runner as runner
    from langgraph.checkpoint.memory import InMemorySaver as MemorySaver

    savers = []

    def make_saver():
        saver = MemorySaver()
        savers.append(saver)
        return saver

    monkeypatch.setattr(runner, "InMemorySaver", make_saver)
    seen_questions = []

    def handler(request):
        body = json.loads(request.content)
        seen_questions.append(body["messages"][-1]["content"])
        return sse_response([chunk(delta={"content": "完成"})])

    client = make_client(handler)
    for case_id, question in (("case-a", "问题 A"), ("case-b", "问题 B")):
        result, _ = await run_case(
            _case(id=case_id, question=question),
            client=client,
            tools=[],
            resolved={},
            trace_dir=tmp_path,
        )
        assert result.passed, result.failed_checks

    assert seen_questions == ["问题 A", "问题 B"]
    assert len(savers) == 2
    assert savers[0] is not savers[1]


async def test_run_case_failure_records_failed_checks(resolved, tools, tmp_path) -> None:
    case = _case(expect_tools_all=["compare_quotes"], must_mention=["不存在的一句话"])
    client = _mock_client(resolved["order_id"])
    result, _ = await run_case(
        case, client=client, tools=tools, resolved=resolved, trace_dir=tmp_path
    )
    assert not result.passed
    assert any("expect_tools_all" in f for f in result.failed_checks)
    assert any("must_mention" in f for f in result.failed_checks)


async def test_run_case_survives_llm_error(resolved, tools, tmp_path) -> None:
    """上游错误不外抛：CaseResult.error 记失败，评测继续（本用例关闭重试保速度）。"""
    from httpx2 import Response

    def handler(request):
        return Response(500, content=b"upstream error")

    case = _case(expect_tools_any=["get_order"])
    result, _ = await run_case(
        case,
        client=make_client(handler),
        tools=tools,
        resolved=resolved,
        trace_dir=tmp_path,
        retries=0,
    )
    assert not result.passed
    assert result.error is not None
    assert any("run_error" in f for f in result.failed_checks)


async def test_run_case_retries_transient_then_succeeds(resolved, tools, tmp_path) -> None:
    """上游 5xx 是基础设施抖动：自动重跑，第二次成功后 attempts=2、照常判分。"""
    from httpx2 import Response

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(500, content=b"upstream error")
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response(
                [
                    chunk(delta={"content": f"订单 {resolved['order_id']} 状态：待发货"}),
                    chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                ]
            )
        return sse_response(
            tool_call_chunks("call_1", "get_order", json.dumps({"order_id": resolved["order_id"]}))
        )

    case = _case(expect_tools_any=["get_order"])
    result, _ = await run_case(
        case,
        client=make_client(handler, max_retries=0),
        tools=tools,
        resolved=resolved,
        trace_dir=tmp_path,
        retries=2,
        backoff=0,
    )
    assert result.passed, result.failed_checks
    assert result.attempts == 2
    assert calls["n"] == 3  # 失败尝试 1 次 + 成功尝试的 2 轮请求（工具轮 + 最终回答）


async def test_run_case_retry_exhausted_records_last_error(resolved, tools, tmp_path) -> None:
    """瞬态重试耗尽后按失败计：attempts = retries + 1，error 是最后一次的。"""
    from httpx2 import Response

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return Response(500, content=b"upstream error")

    case = _case(expect_tools_any=["get_order"])
    result, _ = await run_case(
        case,
        client=make_client(handler, max_retries=0),
        tools=tools,
        resolved=resolved,
        trace_dir=tmp_path,
        retries=2,
        backoff=0,
    )
    assert not result.passed
    assert result.attempts == 3
    assert calls["n"] == 3
    assert any("run_error" in f for f in result.failed_checks)


async def test_run_case_does_not_retry_deterministic_error(resolved, tools, tmp_path) -> None:
    """4xx 是确定性错误：重试无意义，只调一次就判失败。"""
    from httpx2 import Response

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return Response(400, content=b"bad request")

    case = _case(expect_tools_any=["get_order"])
    result, _ = await run_case(
        case,
        client=make_client(handler, max_retries=0),
        tools=tools,
        resolved=resolved,
        trace_dir=tmp_path,
        retries=2,
        backoff=0,
    )
    assert not result.passed
    assert result.attempts == 1
    assert calls["n"] == 1


async def test_budget_circuit_breaker_skips_without_api_call(resolved, tools, tmp_path) -> None:
    """预算耗尽后余下 case 不发请求（handler 一被调就失败）即证明熔断。"""

    def handler(request):
        raise AssertionError("预算耗尽后不应再调 API")

    cases = [_case(expect_tools_any=["get_order"]) for _ in range(3)]
    budget = Budget(limit_cny=0.0)
    results = [
        r
        async for r, _ in run_all(
            cases,
            client=make_client(handler),
            tools=tools,
            resolved=resolved,
            trace_dir=tmp_path,
            budget=budget,
        )
    ]
    assert len(results) == 3
    assert all(not r.passed for r in results)
    assert all(any(f.startswith("budget:") for f in r.failed_checks) for r in results)


async def test_success_claim_cannot_hide_business_error(tmp_path):
    from agent_core.tools import Tool
    from pydantic import BaseModel

    class Args(BaseModel):
        sku: str
        delta: int

    async def broken(args):
        return {"error": {"code": "insufficient_stock", "message": "failed"}}

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response(
                [
                    chunk(delta={"content": "已成功完成"}),
                    chunk(
                        usage={
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        }
                    ),
                ]
            )
        return sse_response(tool_call_chunks("w1", "adjust_stock", '{"sku":"A1","delta":5}'))

    case = _case(expect_successful_tools=["adjust_stock"], must_mention=["成功"])
    result, _ = await run_case(
        case,
        client=make_client(handler),
        tools=[Tool("adjust_stock", "write", Args, broken)],
        resolved={},
        trace_dir=tmp_path,
    )
    assert not result.passed
    assert any("expect_successful_tools" in f for f in result.failed_checks)
    assert result.tool_results[0].arguments == {"sku": "A1", "delta": 5}
    assert result.tool_results[0].content["error"]["code"] == "insufficient_stock"


async def test_expected_business_error_requires_an_observed_tool_result(tmp_path):
    def handler(request):
        return sse_response(
            [
                chunk(delta={"content": "库存不足"}),
                chunk(
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                ),
            ]
        )

    result, _ = await run_case(
        _case(expect_error_codes=["insufficient_stock"], must_mention=["库存不足"]),
        client=make_client(handler),
        tools=[],
        resolved={},
        trace_dir=tmp_path,
    )
    assert not result.passed
    assert any("expect_error_codes" in item for item in result.failed_checks)


async def test_expected_business_error_accepts_matching_tool_result(tmp_path):
    from agent_core.tools import Tool
    from pydantic import BaseModel

    class Args(BaseModel):
        sku: str

    async def reject(args):
        return {"error": {"code": "insufficient_stock", "message": "库存不足"}}

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response(
                [
                    chunk(delta={"content": "库存不足"}),
                    chunk(
                        usage={
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        }
                    ),
                ]
            )
        return sse_response(tool_call_chunks("w1", "adjust_stock", '{"sku":"A1"}'))

    result, _ = await run_case(
        _case(expect_error_codes=["insufficient_stock"], must_mention=["库存不足"]),
        client=make_client(handler),
        tools=[Tool("adjust_stock", "write", Args, reject)],
        resolved={},
        trace_dir=tmp_path,
    )
    assert result.passed, result.failed_checks


async def test_write_run_error_does_not_replay_run_and_keeps_partial_metering(tmp_path):
    from agent_core.approval import RISK_SINGLE_CONFIRM, AutoApproveGate, guarded
    from agent_core.tools import Tool
    from httpx2 import Response
    from pydantic import BaseModel

    class Args(BaseModel):
        pass

    writes = []

    async def write(args):
        writes.append("committed")
        return {"created": True}

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return Response(503, content=b"server_error")
        return sse_response(tool_call_chunks("w1", "write", "{}"))

    result, _ = await run_case(
        _case(),
        client=make_client(handler, max_retries=0),
        tools=[
            guarded(
                Tool("write", "write", Args, write, risk=RISK_SINGLE_CONFIRM), AutoApproveGate()
            )
        ],
        resolved={},
        trace_dir=tmp_path,
        backoff=0,
    )
    assert writes == ["committed"]
    assert not result.passed and result.attempts == 1
    assert result.total_tokens > 0 and result.cost > 0
    assert not result.cost_complete
    assert result.tool_results[0].succeeded


# ---- M06-02：BudgetedClient 集成（预算熔断不改旧 CNY cost 契约） ----


def _billing_rate():
    from decimal import Decimal

    from evals.billing import EndpointRate

    return EndpointRate(
        model="glm-5.3-flash",
        endpoint_host="llm.test",
        currency="USD",
        input_per_m=Decimal("0.045"),
        output_per_m=Decimal("0.18"),
        price_source="test-fixture",
    )


async def test_run_case_records_wrapper_billing_and_keeps_cny_cost(resolved, tools, tmp_path):
    from decimal import Decimal

    from evals.billing import BudgetedClient, CurrencyBudget

    client = BudgetedClient(_mock_client(resolved["order_id"]), _billing_rate(),
                            CurrencyBudget(Decimal("1"), "USD", max_requests=5))
    result, _ = await run_case(_case(), client=client, tools=tools, resolved=resolved,
                               trace_dir=tmp_path)
    assert result.passed, result.failed_checks
    # agent 循环这条 case 有两次模型请求：工具轮 usage(13,7) + 文本轮 usage(10,5)。
    assert result.billing is not None
    assert result.billing.currency == "USD"
    assert result.billing.cache_assumption == "uncached_upper_bound"
    assert Decimal(result.billing.estimated_amount) == Decimal("0.000003")
    assert client.budget.spent == Decimal("0.000003")
    # GLM 的旧 CNY cost 契约保持原样，没有混入 USD 数值。
    assert result.cost is not None and result.cost > 0 and result.cost_complete


async def test_wrapper_budget_exhaustion_blocks_next_case_before_http(resolved, tools, tmp_path):
    from decimal import Decimal

    from evals.billing import BudgetedClient, CurrencyBudget

    calls: list[bool] = []

    def counting_client():
        base = _mock_client(resolved["order_id"])
        original = base.stream_chat

        async def counted(*args, **kwargs):
            calls.append(True)
            async for event in original(*args, **kwargs):
                yield event

        base.stream_chat = counted
        return base

    client = BudgetedClient(counting_client(), _billing_rate(),
                            CurrencyBudget(Decimal("1"), "USD", max_requests=2))
    result, _ = await run_case(_case(), client=client, tools=tools, resolved=resolved,
                               trace_dir=tmp_path)
    assert result.passed  # 首条 case 正好用满 2 次请求（工具轮 + 文本轮）
    blocked, _ = await run_case(_case(), client=client, tools=tools, resolved=resolved,
                                trace_dir=tmp_path)
    assert len(calls) == 2  # 第二条 case 的请求未到 HTTP 层
    assert not blocked.passed and not blocked.completed
    assert "BudgetExhausted" in blocked.error


async def test_missing_usage_marks_billing_unknown_and_blocks_following_case(
    resolved, tools, tmp_path
):
    from decimal import Decimal

    from evals.billing import BudgetedClient, CurrencyBudget

    calls: list[bool] = []

    def handler(request):
        calls.append(True)
        return sse_response([chunk(delta={"content": "完成，但没有回 usage 包"})])

    client = BudgetedClient(make_client(handler), _billing_rate(),
                            CurrencyBudget(Decimal("1"), "USD", max_requests=10))
    first, _ = await run_case(_case(), client=client, tools=tools, resolved=resolved,
                              trace_dir=tmp_path)
    assert first.billing is not None
    assert first.billing.estimated_amount is None
    assert first.billing.cache_assumption == "unknown_usage"
    assert first.billing.complete is False and not first.cost_complete
    second, _ = await run_case(_case(), client=client, tools=tools, resolved=resolved,
                               trace_dir=tmp_path)
    assert len(calls) == 1  # 不知道费用 ≠ 没有费用：后续请求全部停止
    assert "BudgetExhausted" in (second.error or "")
