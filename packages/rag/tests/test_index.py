"""向量索引单测：余弦检索、阈值拒答、top-k、持久化（fake embedder，不烧 token）。"""

import math

import pytest
from rag.chunk import Chunk
from rag.embed import cosine
from rag.index import Hit, PolicyIndex

_VOCAB = ["退货", "折扣", "价格", "库存"]


class FakeEmbedder:
    """确定性词频向量：按固定词表计数，供离线测试，不触网。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(t.count(w)) for w in _VOCAB] for t in texts]


_CHUNKS = [
    Chunk(source="a.md", title="退货", text="退货 退货 的说明"),
    Chunk(source="b.md", title="折扣", text="折扣 的说明"),
    Chunk(source="c.md", title="价格", text="价格 的说明"),
]


def test_cosine_basic() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 1.0], [1.0, 1.0]) == pytest.approx(1.0)


def test_cosine_zero_vector_is_zero() -> None:
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_search_ranks_most_similar_first() -> None:
    index = PolicyIndex.build(_CHUNKS, FakeEmbedder())
    hits = index.search("怎么退货", FakeEmbedder(), k=3)
    assert isinstance(hits[0], Hit)
    assert hits[0].chunk.title == "退货"
    assert hits[0].score == pytest.approx(1.0)


def test_search_top_k_limits_results() -> None:
    index = PolicyIndex.build(_CHUNKS, FakeEmbedder())
    hits = index.search("退货 折扣 价格", FakeEmbedder(), k=2)
    assert len(hits) == 2


def test_search_threshold_filters_out_of_corpus() -> None:
    """语料外查询（零向量）得分 0，低于阈值 → 空结果，支撑拒答。"""
    index = PolicyIndex.build(_CHUNKS, FakeEmbedder())
    hits = index.search("今天天气如何", FakeEmbedder(), k=3, threshold=0.1)
    assert hits == []


def test_search_descending_scores() -> None:
    index = PolicyIndex.build(_CHUNKS, FakeEmbedder())
    hits = index.search("退货 折扣 价格", FakeEmbedder(), k=3)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_save_load_roundtrip(tmp_path) -> None:
    index = PolicyIndex.build(_CHUNKS, FakeEmbedder())
    path = tmp_path / "index.json"
    index.save(path)
    loaded = PolicyIndex.load(path)
    assert [c.title for c in loaded.chunks] == [c.title for c in index.chunks]
    hits = loaded.search("怎么退货", FakeEmbedder(), k=1)
    assert hits[0].chunk.title == "退货"


def test_build_rejects_vector_count_mismatch() -> None:
    class BadEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0]]  # 数量与 chunks 不匹配

    with pytest.raises(ValueError, match="数量不匹配"):
        PolicyIndex.build(_CHUNKS, BadEmbedder())


def test_embedder_vectors_are_finite() -> None:
    vectors = FakeEmbedder().embed(["退货"])
    assert all(math.isfinite(x) for x in vectors[0])
