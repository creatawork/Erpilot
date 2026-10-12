"""Select effective, unambiguous document versions before vector ranking."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from rag.embed import Embedder
from rag.index import Hit, PolicyIndex
from rag.policy import PolicyMetadata

PolicyState = Literal["matched", "no_match", "unavailable", "inactive", "conflict"]


@dataclass
class PolicyEvidence:
    state: PolicyState
    matches: list[Hit]
    reason: str = ""


def query_policy(
    index: PolicyIndex,
    embedder: Embedder,
    query: str,
    *,
    at: datetime,
    k: int = 3,
    threshold: float = 0.66,
    rule_id: str | None = None,
) -> PolicyEvidence:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("查询时间必须带时区")
    documents = []
    try:
        for chunk in index.chunks:
            if chunk.metadata is None or chunk.metadata.source != chunk.source:
                raise ValueError("索引缺少一致的政策元数据")
            metadata = PolicyMetadata.model_validate(chunk.metadata.model_dump())
            if metadata not in documents:
                documents.append(metadata)
        relevant = [m for m in documents if rule_id is None or rule_id in m.rules]
        active = [m for m in relevant if m.active_at(at)]
    except (ValueError, TypeError, AttributeError):
        return PolicyEvidence("unavailable", [], "索引元数据无效，请核对政策目录并刷新索引")
    if not relevant:
        return PolicyEvidence("no_match", [], "政策文档未覆盖所需规则")
    if not active:
        return PolicyEvidence("inactive", [], "没有在查询时点生效的相关政策")
    if len({m.policy_id for m in active}) != len(active):
        return PolicyEvidence("conflict", [], "相关政策存在重叠生效版本或同版本不一致内容")
    pairs = [
        (chunk, vector)
        for chunk, vector in zip(index.chunks, index.vectors, strict=True)
        if chunk.metadata in active
        and (rule_id is None or chunk.title in chunk.metadata.rules[rule_id].titles)
    ]
    if not pairs:
        return PolicyEvidence("no_match", [], "未检索到对应规则的小节")
    try:
        selected = PolicyIndex([c for c, _ in pairs], [v for _, v in pairs])
        hits = selected.search(query, embedder, k=k, threshold=threshold)
    except Exception:
        return PolicyEvidence("unavailable", [], "政策检索暂不可用，请核对索引或稍后重试")
    if not hits:
        return PolicyEvidence("no_match", [], "政策文档未找到相关内容")
    return PolicyEvidence("matched", hits)
