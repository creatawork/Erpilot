import copy
import json

import pytest
from agent_core.display import build_display

RESULT = {
    "schema_version": 1,
    "assessed_at": "2026-10-12T12:00:00Z",
    "order": {"order_id": "SO-T", "customer": "甲", "status": "待付款", "total_amount": 19.5},
    "business_allowed": True,
    "business_reason": {"code": "cancellable_status", "message": "当前状态允许发起取消申请"},
    "policy_state": "matched",
    "decision": "eligible",
    "message": "当前可以发起取消申请，仍需审批；本次仅查询，未执行取消。",
    "references": [
        {
            "source": "order-policy.md",
            "title": "可取消的订单",
            "text": "待付款待发货可取消",
            "score": 0.9,
            "policy_id": "order",
            "version": "2026-10-12.1",
            "effective_from": "2026-10-11T16:00:00Z",
            "effective_until": None,
        }
    ],
}


def display(result, args=None):
    return build_display(
        "assess_order_cancellation", args or {"order_id": "SO-T"}, json.dumps(result), True
    )


def test_assessment_is_query_completion_not_mutation():
    result = display(RESULT)
    assert result is not None
    assert result["kind"] == "cancellation_assessment"
    assert result["outcome"] == "succeeded"
    assert result["assessment"] == RESULT
    assert "operation_id" not in result


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("assessed_at",), "2026-10-12T12:00:00"),
        (("decision",), "approved"),
        (("business_allowed",), 1),
        (("business_allowed",), False),
        (("policy_state",), "unavailable"),
        (("references",), []),
        (("order", "total_amount"), True),
        (("order", "total_amount"), float("nan")),
        (("order", "total_amount"), -1),
        (("order", "status"), "已发货"),
        (("order", "order_id"), "different"),
        (("references", 0, "version"), ""),
        (("references", 0, "score"), float("inf")),
        (("references", 0, "score"), True),
        (("references", 0, "effective_from"), "2027-01-01T00:00:00Z"),
        (("references", 0, "effective_until"), "2026-10-12T12:00:00Z"),
    ],
)
def test_invalid_shape_falls_back(path, value):
    result = copy.deepcopy(RESULT)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    projected = display(result)
    assert projected is None or projected["outcome"] == "unknown"


@pytest.mark.parametrize(
    "allowed,state,decision,status",
    [
        (True, "no_match", "needs_review", "待发货"),
        (True, "conflict", "needs_review", "待付款"),
        (False, "unavailable", "ineligible", "已发货"),
    ],
)
def test_noneligible_queries_still_completed(allowed, state, decision, status):
    result = copy.deepcopy(RESULT)
    result.update(business_allowed=allowed, policy_state=state, decision=decision, references=[])
    result["order"]["status"] = status
    result["business_reason"]["code"] = "cancellable_status" if allowed else "invalid_transition"
    assert display(result)["outcome"] == "succeeded"
