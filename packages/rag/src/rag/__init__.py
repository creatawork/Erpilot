"""rag：业务无关的轻量检索引擎（语料分块 + 嵌入协议 + 余弦向量索引）。"""

from rag.chunk import Chunk, chunk_markdown
from rag.embed import Embedder, cosine
from rag.index import Hit, PolicyIndex

__all__ = ["Chunk", "Embedder", "Hit", "PolicyIndex", "chunk_markdown", "cosine"]
