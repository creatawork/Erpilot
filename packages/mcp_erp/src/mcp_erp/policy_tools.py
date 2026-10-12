"""Read-only policy tools, shared by session and legacy MCP surfaces."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from rag.evidence import query_policy
from rag.index import Hit

CANCELLATION_QUERY = "订单取消条件"


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
