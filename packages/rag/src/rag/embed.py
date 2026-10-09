"""嵌入协议与余弦相似度。

Embedder 是协议：真实实现走 OpenAI 兼容 embeddings 端点，测试注入 fake。
向量用纯 Python list[float]，小语料无需 numpy。
"""

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """把一批文本映射为等长向量。"""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；任一为零向量时返回 0.0（避免除零）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
