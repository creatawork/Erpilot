import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from erp_store.models import Order, OrderStatus
from rag.corpus import load_corpus
from rag.evidence import PolicyEvidence
from rag.index import Hit

AT = datetime(2026, 10, 12, 12, tzinfo=UTC)


def decide(order, evidence):
    assert importlib.util.find_spec("mcp_erp.cancellation_assessment") is not None
    from mcp_erp.cancellation_assessment import decide_cancellation

    return decide_cancellation(order, evidence, assessed_at=AT)


def evidence():
    chunk = next(c for c in load_corpus() if c.title == "可取消的订单")
    return PolicyEvidence("matched", [Hit(chunk=chunk, score=0.9)])


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("state", ["matched", "no_match", "unavailable", "inactive", "conflict"])
def test_status_and_evidence_matrix(status, state):
    order = Order(order_id="SO-T", customer="甲", status=status, created_at=AT, items=[])
    proof = evidence() if state == "matched" else PolicyEvidence(state, [])
    result = decide(order, proof)
    allowed = status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING_SHIPMENT)
    expected = (
        "ineligible" if not allowed else ("eligible" if state == "matched" else "needs_review")
    )
    assert result["decision"] == expected
    assert result["business_allowed"] is allowed
    assert result["schema_version"] == 1
    assert result["assessed_at"].endswith("Z")
    assert "未执行取消" in result["message"]
    assert bool(result["references"]) == (state == "matched")


@pytest.mark.parametrize("change", ["empty", "values", "title", "inactive", "source", "versions"])
def test_invalid_matched_evidence_cannot_grant_eligibility(change):
    proof = evidence()
    chunk = proof.matches[0].chunk
    metadata = chunk.metadata
    if change == "empty":
        proof.matches = []
    elif change == "values":
        metadata.rules["order.cancel"].values = ["已发货"]
    elif change == "title":
        chunk.title = "无关小节"
    elif change == "inactive":
        metadata.effective_from = AT + timedelta(days=1)
    elif change == "source":
        chunk.source = "other.md"
    else:
        other = chunk.model_copy(deep=True)
        other.metadata.version = "overlapping"
        proof.matches.append(Hit(chunk=other, score=0.99))
    order = Order(order_id="SO-T", customer="甲", status="待付款", created_at=AT, items=[])
    result = decide(order, proof)
    assert result["decision"] == "needs_review"
    assert result["references"] == []
