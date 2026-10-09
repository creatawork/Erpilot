"""政策语料：从 policies/*.md 读取并分块，作为检索对象（与业务库分离）。"""

from pathlib import Path

from rag.chunk import Chunk, chunk_markdown

POLICIES_DIR = Path(__file__).parent / "policies"
DEFAULT_INDEX_PATH = Path(__file__).parent / "policy_index.json"


def load_corpus(policies_dir: Path = POLICIES_DIR) -> list[Chunk]:
    """读取政策目录下全部 .md，按标题分块；文件名作 source。顺序稳定（按文件名）。"""
    chunks: list[Chunk] = []
    for path in sorted(Path(policies_dir).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        chunks.extend(chunk_markdown(text, source=path.name))
    return chunks
