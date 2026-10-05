import json

import pytest
from agent_core.approval import AutoApproveGate, AutoDenyGate
from agent_core.testing import USAGE, chunk, make_client, multi_tool_chunks, sse_response
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from evals.approval_cases import APPROVAL_CASES
from evals.runner import run_case
from mcp_erp import build_agent_tools_async


@pytest.mark.parametrize("actual_delta,repeat,approved,expected_pass", [
    (5, 1, True, True), (4, 1, True, False), (5, 2, True, False), (5, 1, False, False),
])
async def test_approved_stock_case_checks_exact_database_change(
    tmp_path, actual_delta, repeat, approved, expected_pass,
):
    db = tmp_path / "erp.db"
    seed_database(db, n_products=60, n_orders=80)
    engine = make_engine(db)
    from evals.context import resolve

    resolved = resolve(ErpRepository(engine))
    gate = AutoApproveGate() if approved else AutoDenyGate()
    tools = await build_agent_tools_async(db, writes=True, approval_gate=gate)

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "已成功完成"}), chunk(usage=USAGE)])
        args = json.dumps({"sku": resolved["on_sale_sku"], "delta": actual_delta})
        return sse_response(multi_tool_chunks([
            (f"w{i}", "adjust_stock", args) for i in range(repeat)
        ]))

    result, _ = await run_case(
        APPROVAL_CASES[0], client=make_client(handler), tools=tools, resolved=resolved,
        trace_dir=tmp_path / "traces", state_engine=engine,
    )
    assert result.passed is expected_pass, result.failed_checks


async def test_state_expectation_requires_database(tmp_path):
    client = make_client(lambda request: sse_response([
        chunk(delta={"content": "已成功完成"}), chunk(usage=USAGE),
    ]))
    result, _ = await run_case(
        APPROVAL_CASES[0], client=client, tools=[], resolved={"on_sale_sku": "A1"},
        trace_dir=tmp_path,
    )
    assert any("state: 缺少" in f for f in result.failed_checks)
