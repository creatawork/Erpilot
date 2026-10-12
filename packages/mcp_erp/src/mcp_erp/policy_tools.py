"""Read-only policy tools, shared by session and legacy MCP surfaces."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from rag.evidence import PolicyEvidence, query_policy
from rag.index import Hit

CANCELLATION_QUERY = "订单取消条件"
CANCELLATION_GUIDANCE = (
    "用户询问某订单能否取消、是否符合取消条件时，先用只读工具 assess_order_cancellation；"
    "缺少明确订单号或有多个候选订单时先向用户确认，不能猜测最新一单。"
    "资格询问只查询，不创建取消预览、不申请审批、不执行取消。"
    "按返回的订单事实、decision、policy_state 和真实版本引用解释；"
    "eligible 仅表示可发起申请，仍需审批；needs_review 须说明政策依据缺口，"
    "ineligible 须说明当前状态禁止取消，不能承诺退款。"
    "本次仅查询，未执行取消。只有用户明确要求取消时才进入当前可用写工具的审批流程；"
    "查询结果不代替审批或执行时的状态校验。"
)


def reference(hit: Hit) -> dict:
    metadata = hit.chunk.metadata
    if metadata is None:
        raise ValueError("政策缺少元数据")
    data = metadata.model_dump(mode="json")
    return {
        "source": hit.chunk.source,
        "title": hit.chunk.title,
        "text": hit.chunk.text,
        "score": round(hit.score, 4),
        **{
            key: data[key]
            for key in (
                "policy_id",
                "version",
                "effective_from",
                "effective_until",
            )
        },
    }


def register_policy_tools(mcp, repo, resolver, threshold):
    from mcp_erp.server import _err

    @mcp.tool
    def assess_order_cancellation(
        order_id: Annotated[str, Field(description="需要评估的完整订单号；不确定时先询问用户")],
    ) -> dict:
        """只读判断订单能否发起取消申请：返回当前订单事实、政策版本依据及缺口。

        不创建预览、不请求审批、不取消订单、不执行退款；缺少明确单号时先澄清。
        """
        from mcp_erp.cancellation_assessment import decide_cancellation

        order_id = order_id.strip()
        if not order_id:
            return _err("invalid_argument", "订单号不能为空", "请提供完整订单号")
        order = repo.get_order(order_id)
        if order is None:
            return _err("not_found", f"订单不存在：{order_id}", "请确认完整订单号")
        at = datetime.now(UTC)
        try:
            index, embedder = resolver()
            evidence = query_policy(
                index,
                embedder,
                CANCELLATION_QUERY,
                at=at,
                threshold=threshold,
                rule_id="order.cancel",
            )
        except Exception:
            evidence = PolicyEvidence("unavailable", [])
        return decide_cancellation(order, evidence, assessed_at=at)

    @mcp.tool
    def search_policy(
        query: Annotated[str, Field(description="政策相关问题或关键词，如退货、折扣、取消")],
        k: Annotated[int, Field(description="返回片段数（1~5）")] = 3,
    ) -> dict:
        """检索有效政策片段及来源、版本、生效时间；无命中时如实说明未覆盖，不要臆造。"""
        if not query.strip():
            return _err("invalid_argument", "查询不能为空", "例如：满多少件有折扣、几天内可退货")
        if not 1 <= k <= 5:
            return _err("invalid_argument", "k 须在 1~5")
        try:
            index, embedder = resolver()
            evidence = query_policy(
                index, embedder, query.strip(), at=datetime.now(UTC), k=k, threshold=threshold
            )
        except Exception:
            evidence = None
        if evidence is None or evidence.state == "unavailable":
            return _err(
                "policy_unavailable",
                "政策检索未就绪，政策或索引可能已更新",
                "请核对政策目录与嵌入配置，运行 python -m rag build 或 rag refresh",
            )
        return {
            "policy_state": evidence.state,
            "matches": [reference(hit) for hit in evidence.matches],
            "note": evidence.reason
            + ("；请如实说明，不要臆造政策" if not evidence.matches else ""),
        }
