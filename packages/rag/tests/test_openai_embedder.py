"""嵌入器单测：env 配置解析 + 注入 fake client 验证映射/排序（不触网，不烧 token）。"""

from dataclasses import dataclass

import pytest
from rag.openai_embedder import (
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_EMBED_MODEL,
    EmbedConfig,
    OpenAIEmbedder,
)


@dataclass
class _Item:
    index: int
    embedding: list[float]


@dataclass
class _Response:
    data: list[_Item]


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, *, model: str, input: list[str]) -> _Response:  # noqa: A002
        self.calls.append({"model": model, "input": input})
        # 故意乱序返回，验证 embedder 按 index 重排
        return _Response(data=[
            _Item(index=i, embedding=[float(len(t)), float(i)])
            for i, t in reversed(list(enumerate(input)))
        ])


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()


def _clear_embed_env(monkeypatch) -> None:
    for name in ("EMBED_API_KEY", "EMBED_API_KEY_ENV", "EMBED_MODEL", "EMBED_BASE_URL",
                 "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_from_env_defaults_to_zhipu(monkeypatch) -> None:
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    config = EmbedConfig.from_env()
    assert config.api_key == "zk"
    assert config.model == DEFAULT_EMBED_MODEL
    assert config.base_url == DEFAULT_EMBED_BASE_URL


def test_from_env_explicit_embed_key_wins(monkeypatch) -> None:
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    monkeypatch.setenv("EMBED_API_KEY", "ek")
    assert EmbedConfig.from_env().api_key == "ek"


def test_from_env_model_and_base_url_override(monkeypatch) -> None:
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("EMBED_API_KEY", "ek")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v3")
    monkeypatch.setenv("EMBED_BASE_URL", "https://dashscope.example/v1")
    config = EmbedConfig.from_env()
    assert config.model == "text-embedding-v3"
    assert config.base_url == "https://dashscope.example/v1"


def test_from_env_missing_key_raises(monkeypatch) -> None:
    _clear_embed_env(monkeypatch)
    with pytest.raises(RuntimeError, match="ZHIPU_API_KEY"):
        EmbedConfig.from_env()


def test_embed_maps_and_reorders_by_index() -> None:
    client = _FakeClient()
    embedder = OpenAIEmbedder(EmbedConfig(api_key="k"), client=client)
    vectors = embedder.embed(["ab", "abcd"])
    # index 0 对应 "ab"(len2)，index 1 对应 "abcd"(len4)；重排后顺序与输入一致
    assert vectors == [[2.0, 0.0], [4.0, 1.0]]
    assert client.embeddings.calls[0]["input"] == ["ab", "abcd"]


def test_embed_empty_input_returns_empty() -> None:
    client = _FakeClient()
    embedder = OpenAIEmbedder(EmbedConfig(api_key="k"), client=client)
    assert embedder.embed([]) == []
    assert client.embeddings.calls == []  # 空输入不触网
