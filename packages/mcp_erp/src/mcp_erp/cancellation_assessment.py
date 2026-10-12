"""Combine authoritative order state with explanatory policy evidence; no writes."""

from datetime import UTC, datetime

from erp_store.models import Order
from erp_store.mutations import CANCELLABLE_STATUSES
from rag.evidence import PolicyEvidence
from rag.policy import PolicyMetadata

from mcp_erp.policy_tools import reference


def decide_cancellation(order: Order, evidence: PolicyEvidence, *, assessed_at: datetime) -> dict:
    if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
        raise ValueError("评估时间必须带时区")
    allowed = order.status in CANCELLABLE_STATUSES
    state = evidence.state
    if state == "matched":
        if not evidence.matches:
            state = "no_match"
        else:
            documents = []
            try:
                for hit in evidence.matches:
                    metadata = PolicyMetadata.model_validate(hit.chunk.metadata.model_dump())
                    rule = metadata.rules.get("order.cancel")
                    if (
                        metadata.source != hit.chunk.source
                        or not metadata.active_at(assessed_at)
                        or rule is None
                        or hit.chunk.title not in rule.titles
                        or set(rule.values) != {s.value for s in CANCELLABLE_STATUSES}
                    ):
                        raise ValueError("证据与取消规则不一致")
                    if metadata not in documents:
                        documents.append(metadata)
                if len({m.policy_id for m in documents}) != len(documents):
                    raise ValueError("政策版本冲突")
            except (ValueError, TypeError, AttributeError):
                state = "conflict"
    decision = "ineligible" if not allowed else "eligible" if state == "matched" else "needs_review"
    reasons = {
        "no_match": "未找到支持取消规则的政策依据",
        "unavailable": "政策检索暂不可用",
        "inactive": "没有当前生效的相关政策",
        "conflict": "政策版本或规则存在冲突",
    }
    message = (
        "当前可以发起取消申请，仍需审批"
        if decision == "eligible"
        else "当前状态不可直接取消；此操作不执行退款"
        if decision == "ineligible"
        else f"{reasons.get(state, '政策依据待核对')}，需人工核对后再申请"
    )
    return {
        "schema_version": 1,
        "assessed_at": assessed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "order": {
            "order_id": order.order_id,
            "customer": order.customer,
            "status": order.status.value,
            "total_amount": order.total_amount,
        },
        "business_allowed": allowed,
        "business_reason": {
            "code": "cancellable_status" if allowed else "invalid_transition",
            "message": "当前状态允许发起取消申请"
            if allowed
            else "当前状态不可直接取消；此操作不执行退款",
        },
        "policy_state": state,
        "references": [reference(hit) for hit in evidence.matches] if state == "matched" else [],
        "decision": decision,
        "message": message + "；本次仅查询，未执行取消。",
    }
