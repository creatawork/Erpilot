"""政策语料：从 policies/*.md 读取并分块，作为检索对象（与业务库分离）。"""

import json
from pathlib import Path

from rag.chunk import Chunk, chunk_markdown
from rag.policy import PolicyMetadata, policy_digest

POLICIES_DIR = Path(__file__).parent / "policies"
DEFAULT_INDEX_PATH = Path(__file__).parent / "policy_index.json"


def load_corpus(policies_dir: Path = POLICIES_DIR) -> list[Chunk]:
    """读取政策目录下全部 .md，按标题分块；文件名作 source。顺序稳定（按文件名）。"""
    directory = Path(policies_dir).resolve()
    catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or set(catalog) != {"schema_version", "documents"}:
        raise ValueError("政策目录格式不正确")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise ValueError("政策目录版本不支持")
    if not isinstance(catalog["documents"], list):
        raise ValueError("政策目录 documents 须为列表")
    metadata = {}
    for record in catalog["documents"]:
        item = PolicyMetadata.model_validate(record)
        if item.source in metadata:
            raise ValueError("政策来源重复")
        metadata[item.source] = item
    paths = sorted(directory.glob("*.md"))
    if set(metadata) != {path.name for path in paths}:
        raise ValueError("政策文件与目录项不一致")
    chunks: list[Chunk] = []
    for path in paths:
        if path.resolve().parent != directory:
            raise ValueError("政策文件不能位于目录外")
        text = path.read_text(encoding="utf-8")
        item = metadata[path.name]
        if policy_digest(text) != item.source_sha256:
            raise ValueError(f"政策正文哈希不一致：{path.name}")
        sections = chunk_markdown(text, source=path.name)
        titles = {chunk.title for chunk in sections}
        if any(not set(rule.titles) <= titles for rule in item.rules.values()):
            raise ValueError(f"政策规则小节不存在：{path.name}")
        chunks.extend(chunk.model_copy(update={"metadata": item}) for chunk in sections)
    return chunks
