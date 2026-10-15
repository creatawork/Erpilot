"""真实模型验收 harness（M06-06）：固定查询向量与投毒/计量装配。"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["CACHE_FILENAME", "B5Harness", "FixedQueryEmbedder", "build_b5_harness"]

CACHE_FILENAME = "b5-query-vector.json"


class FixedQueryEmbedder:
    """真实运行 harness 的嵌入闸：仅允许对固定查询发起 ≤max_requests 次真实请求。

    - 相同缓存目录的下一次运行直接复用缓存向量：两份模型报告共享同一查询向量
      与索引，不各自重复花费嵌入请求；
    - 其他未缓存查询直接拒绝，不允许 harness 之外"偷偷追加请求"；
    - 计量从 inner 的 last_tokens（OpenAIEmbedder 暴露）读取；嵌入价格未确认，
      成本留给报告标注未知。
    """

    def __init__(
        self, inner, fixed_query: str, stats: dict, *,
        cache_dir: Path | None = None, max_requests: int = 1,
    ) -> None:
        self._inner = inner
        self._fixed_query = fixed_query
        self._stats = stats
        self._max_requests = max_requests
        self._vectors: dict[str, list[float]] = {}
        self._path: Path | None = None
        if cache_dir is not None:
            self._path = Path(cache_dir) / CACHE_FILENAME
            self._load_cache()

    def _load_cache(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError:
            return  # 缓存损坏：视同不存在，宁可重新请求也不复用脏向量
        if payload.get("query") == self._fixed_query and payload.get("vector"):
            self._vectors[self._fixed_query] = list(payload["vector"])
            self._stats["shared_with_previous_run"] = True
            self._stats["cache_query_vector_sha256"] = payload.get("vector_sha256")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        unknown = [t for t in texts if t != self._fixed_query]
        if unknown:
            raise ValueError(
                f"未缓存查询 {unknown!r} 不得追加嵌入请求（harness 限制为固定查询）"
            )
        cached = self._vectors.get(self._fixed_query)
        if cached is not None:
            return [list(cached) for _ in texts]
        if self._stats.get("requests", 0) >= self._max_requests:
            raise ValueError(
                f"嵌入请求已达上限 {self._max_requests}：固定查询向量必须复用"
            )
        vectors = [list(v) for v in self._inner.embed(texts)]
        tokens = getattr(self._inner, "last_tokens", None)
        self._stats["requests"] = self._stats.get("requests", 0) + 1
        if tokens is not None:
            self._stats["tokens"] = self._stats.get("tokens", 0) + int(tokens)
        else:
            self._stats["tokens_unknown"] = True
        self._vectors[self._fixed_query] = vectors[0]
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            vector_sha = hashlib.sha256(
                json.dumps(vectors[0]).encode("utf-8")
            ).hexdigest()
            self._stats["cache_query_vector_sha256"] = vector_sha
            self._path.write_text(
                json.dumps(
                    {
                        "query": self._fixed_query,
                        "model": self._stats.get("model"),
                        "tokens": self._stats.get("tokens"),
                        "requests": self._stats.get("requests"),
                        "vector_sha256": vector_sha,
                        "vector": vectors[0],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return vectors


@dataclass
class B5Harness:
    """b5 真实运行装配：政策 resolver + 嵌入计量 + 投毒 case 记录。"""

    policy_resolver: object
    embed_stats: dict = field(default_factory=dict)
    poison_case_ids: frozenset[str] = frozenset()
    poison_records: dict = field(default_factory=dict)


def build_b5_harness(cache_dir: Path | None = None) -> B5Harness:
    """为真实 B5 运行构建 harness：磁盘已有真实索引 + 固定查询向量限制。

    索引以磁盘上的已构建版本为准（不重建、不嵌入语料）；嵌入仅允许
    CANCELLATION_QUERY 一次真实请求并跨运行共享。
    """
    from mcp_erp.policy_tools import CANCELLATION_QUERY
    from rag.corpus import DEFAULT_INDEX_PATH, load_corpus
    from rag.index import PolicyIndex
    from rag.openai_embedder import EmbedConfig, OpenAIEmbedder

    index = PolicyIndex.load(DEFAULT_INDEX_PATH)
    if index.chunks != load_corpus():
        raise RuntimeError(
            "政策文档已更新，索引需要重新构建：uv run --package rag python -m rag build"
        )
    inner = OpenAIEmbedder(EmbedConfig.from_env())
    stats: dict = {
        "model": inner.config.model,
        "requests": 0,
        "tokens": 0,
        "query": CANCELLATION_QUERY,
        "note": "嵌入价格未确认：仅记录用量，成本标未知；不含 LLM 预算",
    }
    embedder = FixedQueryEmbedder(inner, CANCELLATION_QUERY, stats, cache_dir=cache_dir)
    return B5Harness(
        policy_resolver=lambda: (index, embedder),
        embed_stats=stats,
        poison_case_ids=frozenset({"b5-injected-result"}),
    )
