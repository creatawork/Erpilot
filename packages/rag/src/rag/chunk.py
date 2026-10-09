"""语料分块：按 markdown 标题切成片段，标题作面包屑，正文不含标题行。

小语料用标题切片足够：每个 #/## 小节一个片段，便于回答时标明来源小节。
"""

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """一个检索片段：来源文档、小节标题、正文。"""

    source: str = Field(description="来源文档标识，如文件名")
    title: str = Field(description="小节标题（无标题的前言用 source 兜底）")
    text: str = Field(description="片段正文（不含标题行）")


def _heading_title(line: str) -> str | None:
    """markdown 标题行返回其文字，否则 None。"""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    return None


def chunk_markdown(text: str, *, source: str) -> list[Chunk]:
    """按标题把 markdown 切成片段；标题行作 title，正文为其下内容。

    首个标题之前的前言（若有正文）单独成片，title 用 source 兜底。
    正文为空的小节跳过。
    """
    chunks: list[Chunk] = []
    title = source
    body: list[str] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            chunks.append(Chunk(source=source, title=title, text=content))

    for line in text.splitlines():
        heading = _heading_title(line)
        if heading is not None:
            flush()
            title = heading
            body = []
        else:
            body.append(line)
    flush()
    return chunks
