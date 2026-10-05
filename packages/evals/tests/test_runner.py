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
            return sse_response([
                chunk(delta={"content": f"订单 {order_id} 状态：待发货"}),
                chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            ])
        return sse_response(
            tool_call_chunks("call_1", "get_order", json.dumps({"order_id": order_id}))
        )

    return make_client(handler)


async def test_run_case_passes_on_tool_and_text(
    resolved, tools, tmp_path: Path
) -> None:
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
        case, client=make_client(handler), tools=tools, resolved=resolved, trace_dir=tmp_path,
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
            return sse_response([
                chunk(delta={"content": f"订单 {resolved['order_id']} 状态：待发货"}),
                chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            ])
        return sse_response(
            tool_call_chunks("call_1", "get_order", json.dumps({"order_id": resolved["order_id"]}))
        )

    case = _case(expect_tools_any=["get_order"])
    result, _ = await run_case(
        case, client=make_client(handler, max_retries=0), tools=tools, resolved=resolved,
        trace_dir=tmp_path, retries=2, backoff=0,
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
        case, client=make_client(handler, max_retries=0), tools=tools, resolved=resolved,
        trace_dir=tmp_path, retries=2, backoff=0,
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
        case, client=make_client(handler, max_retries=0), tools=tools, resolved=resolved,
        trace_dir=tmp_path, retries=2, backoff=0,
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
        r async for r, _ in run_all(
            cases, client=make_client(handler), tools=tools,
            resolved=resolved, trace_dir=tmp_path, budget=budget,
        )
    ]
    assert len(results) == 3
    assert all(not r.passed for r in results)
    assert all(any(f.startswith("budget:") for f in r.failed_checks) for r in results)
