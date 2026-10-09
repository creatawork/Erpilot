"""分块单测：按 markdown 标题切片，标题作面包屑，正文不含标题行。"""

from rag.chunk import chunk_markdown

_DOC = """# 退换货政策

本店退换货总则。

## 七天无理由退货

签收后 7 天内可申请，商品需保持完好。

## 质量问题

质量问题全程包邮退换。
"""


def test_chunk_splits_by_section_headings() -> None:
    chunks = chunk_markdown(_DOC, source="return-policy.md")
    titles = [c.title for c in chunks]
    assert titles == ["退换货政策", "七天无理由退货", "质量问题"]


def test_chunk_text_excludes_heading_line() -> None:
    chunks = chunk_markdown(_DOC, source="return-policy.md")
    seven_day = next(c for c in chunks if c.title == "七天无理由退货")
    assert "签收后 7 天内可申请" in seven_day.text
    assert "## 七天无理由退货" not in seven_day.text


def test_chunk_records_source() -> None:
    chunks = chunk_markdown(_DOC, source="return-policy.md")
    assert all(c.source == "return-policy.md" for c in chunks)


def test_chunk_skips_empty_sections() -> None:
    doc = "# 标题\n\n## 空小节\n\n## 有内容\n\n正文。\n"
    chunks = chunk_markdown(doc, source="x.md")
    # 「标题」与「空小节」无正文，应跳过；只留有内容的小节
    assert [c.title for c in chunks] == ["有内容"]


def test_chunk_handles_text_before_first_heading() -> None:
    doc = "开场白，没有标题。\n\n## 小节\n\n正文。\n"
    chunks = chunk_markdown(doc, source="x.md")
    assert chunks[0].title == "x.md"  # 无标题的前言用 source 作标题
    assert "开场白" in chunks[0].text
