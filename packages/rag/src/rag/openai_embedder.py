"""真实嵌入器：OpenAI 兼容 embeddings 端点。

与 chat 模型解耦：嵌入有独立 provider/model/key（并非所有 chat 供应商都提供
embeddings）。默认走智谱 embedding-3；可用 EMBED_* 环境变量覆盖。
"""

import os
from dataclasses import dataclass

from openai import OpenAI

DEFAULT_EMBED_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_EMBED_MODEL = "embedding-3"
DEFAULT_EMBED_KEY_ENV = "ZHIPU_API_KEY"
EMBED_BATCH_SIZE = 10


@dataclass(frozen=True, slots=True)
class EmbedConfig:
    api_key: str
    model: str = DEFAULT_EMBED_MODEL
    base_url: str = DEFAULT_EMBED_BASE_URL

    @classmethod
    def from_env(cls) -> "EmbedConfig":
        key_env = os.environ.get("EMBED_API_KEY_ENV", DEFAULT_EMBED_KEY_ENV)
        api_key = os.environ.get("EMBED_API_KEY") or os.environ.get(key_env, "")
        if not api_key:
            raise RuntimeError(
                f"缺少嵌入 API key（EMBED_API_KEY 或 {key_env}）：请在 .env 中填入"
            )
        return cls(
            api_key=api_key,
            model=os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL),
            base_url=os.environ.get("EMBED_BASE_URL", DEFAULT_EMBED_BASE_URL),
        )


class OpenAIEmbedder:
    """用 OpenAI 兼容 embeddings 端点把文本批量转向量。"""

    def __init__(self, config: EmbedConfig, client: OpenAI | None = None) -> None:
        self._config = config
        self._client = client or OpenAI(api_key=config.api_key, base_url=config.base_url)

    @property
    def config(self) -> EmbedConfig:
        return self._config

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本批量转向量；本次调用的请求数与 usage tokens 记到 last_* 属性。

        嵌入价格未在仓库价目表确认：tokens 如实记录，成本留给调用方标注未知。
        """
        self.last_requests = 0
        self.last_tokens: int | None = 0
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            response = self._client.embeddings.create(model=self._config.model, input=batch)
            self.last_requests += 1
            usage = getattr(response, "usage", None)
            total = getattr(usage, "total_tokens", None) if usage is not None else None
            if total is None:
                self.last_tokens = None
            elif self.last_tokens is not None:
                self.last_tokens += int(total)
            # 每个响应的 index 从 0 开始；先在批次内排序再按批次拼接。
            items = sorted(response.data, key=lambda d: d.index)
            vectors.extend(list(item.embedding) for item in items)
        return vectors
