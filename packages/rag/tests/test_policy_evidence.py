import importlib
import importlib.util
from datetime import UTC, datetime

import pytest
from rag.chunk import Chunk
from rag.corpus import load_corpus
from rag.index import PolicyIndex

AT = datetime(2026, 10, 12, 12, tzinfo=UTC)


class Embedder:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def query(chunks=None, *, vectors=None, embedder=None, **kwargs):
    assert importlib.util.find_spec("rag.evidence"), "有效版本检索尚未实现"
    chunks = load_corpus() if chunks is None else chunks
    index = PolicyIndex(chunks, vectors or [[1.0, 0.0] for _ in chunks])
    return importlib.import_module("rag.evidence").query_policy(
        index,
        embedder or Embedder(),
        "订单取消条件",
        at=AT,
        **kwargs,
    )


def order_chunks():
    return [c for c in load_corpus() if c.source == "order-policy.md"]


def test_sections_are_not_versions():
    result = query(rule_id="order.cancel")
    assert result.state == "matched"
    assert {h.chunk.title for h in result.matches} == {"可取消的订单", "已发货或已签收的订单"}
    assert all(h.chunk.metadata.version == "2026-10-12.1" for h in result.matches)


def test_conflict_precedes_top_k():
    chunks = order_chunks()
    other = [
        c.model_copy(update={"metadata": c.metadata.model_copy(update={"version": "v2"})})
        for c in chunks
    ]
    result = query(chunks + other, k=1, rule_id="order.cancel")
    assert result.state == "conflict" and result.matches == []


def test_same_version_different_hash_is_conflict():
    chunks = order_chunks()
    other = chunks[0].model_copy(
        update={
            "metadata": chunks[0].metadata.model_copy(update={"source_sha256": "a" * 64}),
        }
    )
    assert query([*chunks, other], rule_id="order.cancel").state == "conflict"


@pytest.mark.parametrize(
    "change",
    [
        {"effective_from": datetime(2099, 1, 1, tzinfo=UTC)},
        {"effective_until": AT},
    ],
)
def test_inactive_evidence_is_not_cited(change):
    chunks = [
        c.model_copy(update={"metadata": c.metadata.model_copy(update=change)})
        for c in order_chunks()
    ]
    result = query(chunks, rule_id="order.cancel")
    assert result.state == "inactive" and result.matches == []


def test_expired_high_score_cannot_displace_effective_lower_score():
    chunk = next(c for c in order_chunks() if c.title == "可取消的订单")
    expired = chunk.model_copy(
        update={
            "metadata": chunk.metadata.model_copy(
                update={
                    "version": "old",
                    "effective_until": AT,
                }
            )
        }
    )
    result = query([expired, chunk], vectors=[[1.0, 0.0], [0.8, 0.6]], k=1, rule_id="order.cancel")
    assert result.state == "matched"
    assert result.matches[0].chunk.metadata.version == "2026-10-12.1"
    assert result.matches[0].score == pytest.approx(0.8)


def test_low_scores_and_unrelated_sections_do_not_support_rule():
    assert (
        query(vectors=[[0.0, 1.0] for _ in load_corpus()], rule_id="order.cancel").state
        == "no_match"
    )
    unrelated = [c for c in order_chunks() if c.title == "订单状态"]
    assert query(unrelated, rule_id="order.cancel").state == "no_match"
    assert query(rule_id="unknown.rule").state == "no_match"


def test_old_index_and_mismatched_sources_are_unavailable():
    assert query([Chunk(source="old.md", title="旧", text="旧正文")]).state == "unavailable"
    chunk = order_chunks()[0].model_copy(update={"source": "other.md"})
    assert query([chunk]).state == "unavailable"


def test_embedding_failure_is_sanitized():
    class Failed:
        def embed(self, texts):
            raise RuntimeError("sensitive-upstream-detail")

    result = query(embedder=Failed(), rule_id="order.cancel")
    assert result.state == "unavailable" and result.matches == []
    assert "sensitive-upstream-detail" not in result.reason


def test_invalid_query_timezone_is_rejected():
    assert importlib.util.find_spec("rag.evidence")
    with pytest.raises(ValueError, match="时区"):
        importlib.import_module("rag.evidence").query_policy(
            PolicyIndex.build(load_corpus(), Embedder()),
            Embedder(),
            "政策",
            at=AT.replace(tzinfo=None),
        )


def test_incompatible_vector_spaces_are_unavailable():
    class DifferentDimension:
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    result = query(embedder=DifferentDimension(), rule_id="order.cancel")
    assert result.state == "unavailable" and result.matches == []
