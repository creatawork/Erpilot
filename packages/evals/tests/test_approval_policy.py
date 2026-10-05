"""ScriptedPolicyGate 单测：规则表秒批/秒拒、决策留痕（M4 第 2 周）。"""

import pytest
from agent_core.approval import RISK_BATCH_CONFIRM, RISK_SINGLE_CONFIRM
from evals.approval_policy import STOCK_DELTA_LIMIT, ScriptedPolicyGate


def _request(tool: str, risk: str, arguments: dict):
    from agent_core.approval import ApprovalRequest

    return ApprovalRequest(tool=tool, risk=risk, arguments=arguments)


@pytest.mark.parametrize(
    ("tool", "risk", "arguments"),
    [
        ("adjust_stock", RISK_BATCH_CONFIRM, {"sku": "A1001", "delta": 5}),
        ("adjust_stock", RISK_BATCH_CONFIRM, {"sku": "A1001", "delta": -50}),
        ("set_product_status", RISK_BATCH_CONFIRM, {"sku": "A1001", "status": "on_sale"}),
    ],
)
async def test_low_risk_within_limit_approved(tool, risk, arguments) -> None:
    gate = ScriptedPolicyGate()
    decision = await gate.review(_request(tool, risk, arguments))
    assert decision.approved is True
    assert gate.decisions[-1][0].arguments == arguments  # 决策留痕


async def test_stock_delta_over_limit_denied() -> None:
    gate = ScriptedPolicyGate()
    decision = await gate.review(
        _request("adjust_stock", RISK_BATCH_CONFIRM, {"sku": "A1001", "delta": 200})
    )
    assert decision.approved is False
    assert "±50" in decision.reason or str(STOCK_DELTA_LIMIT) in decision.reason


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_order", {"customer": "拾光杂货", "items": []}),
        ("cancel_order", {"order_id": "SO20260301-0001"}),
    ],
)
async def test_single_confirm_always_denied(tool, arguments) -> None:
    gate = ScriptedPolicyGate()
    decision = await gate.review(_request(tool, RISK_SINGLE_CONFIRM, arguments))
    assert decision.approved is False
    assert "审批" in decision.reason
