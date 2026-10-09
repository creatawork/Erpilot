"""政策语料加载单测：真实 .md 分块、来源标注、关键规则在库（确保与业务口径一致）。"""

from rag.corpus import load_corpus


def test_load_corpus_returns_chunks() -> None:
    chunks = load_corpus()
    assert len(chunks) >= 9  # 三份政策、每份多个小节
    assert all(c.text.strip() for c in chunks)


def test_sources_are_policy_filenames() -> None:
    sources = {c.source for c in load_corpus()}
    assert sources == {"order-policy.md", "pricing-policy.md", "return-policy.md"}


def test_discount_tiers_are_grounded() -> None:
    """折扣档位必须与 erp_store.repository.QUOTE_TIERS 一致。"""
    text = "\n".join(c.text for c in load_corpus())
    assert "0.90" in text and "0.95" in text and "0.98" in text
    assert "≥ 200" in text and "≥ 50" in text and "≥ 10" in text


def test_cancel_and_return_rules_present() -> None:
    titles = {c.title for c in load_corpus()}
    assert "可取消的订单" in titles
    assert "七天无理由退货" in titles
