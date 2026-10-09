"""向量索引：余弦 top-k 检索 + 阈值拒答 + JSON 持久化。

阈值的作用是支撑「语料外拒答」：所有片段相似度都低于阈值时返回空，
调用方据此如实说明「政策文档里没有相关内容」，而不是臆造答案。
"""

import json
from pathlib import Path

from pydantic import BaseModel

from rag.chunk import Chunk
from rag.embed import Embedder, cosine


class Hit(BaseModel):
    """一条检索命中：片段 + 相似度得分。"""

    chunk: Chunk
    score: float


class PolicyIndex:
    """片段 + 对齐向量的内存索引；嵌入一次后可持久化复用。"""

    def __init__(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不匹配")
        self.chunks = chunks
        self.vectors = vectors

    @classmethod
    def build(cls, chunks: list[Chunk], embedder: Embedder) -> "PolicyIndex":
        vectors = embedder.embed([c.text for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("embedder 返回向量数量不匹配 chunks")
        return cls(chunks, vectors)

    def search(
        self, query: str, embedder: Embedder, *, k: int = 3, threshold: float = 0.0
    ) -> list[Hit]:
        """检索与 query 最相似的 k 个片段（得分 ≥ threshold）；无命中返回空。"""
        query_vec = embedder.embed([query])[0]
        scored = [
            Hit(chunk=chunk, score=cosine(query_vec, vec))
            for chunk, vec in zip(self.chunks, self.vectors, strict=True)
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored if h.score >= threshold][:k]

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chunks": [c.model_dump() for c in self.chunks],
            "vectors": self.vectors,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "PolicyIndex":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        chunks = [Chunk.model_validate(c) for c in payload["chunks"]]
        return cls(chunks, payload["vectors"])
