import importlib

import pytest
from rag.chunk import Chunk
from rag.corpus import load_corpus
from rag.index import PolicyIndex


def old_index():
    chunks = load_corpus()
    return PolicyIndex(
        [Chunk(source=c.source, title=c.title, text=c.text) for c in chunks],
        [[1.0, 0.0] for _ in chunks],
    )


def refresh(index, chunks):
    fn = getattr(importlib.import_module("rag.index"), "refresh_index_metadata", None)
    assert fn is not None, "索引元数据刷新尚未实现"
    return fn(index, chunks)


def test_refresh_reuses_verified_vectors():
    old = old_index()
    new = refresh(old, load_corpus())
    assert new.vectors == old.vectors
    assert all(c.metadata for c in new.chunks)
    assert all(c.metadata is None for c in old.chunks)


@pytest.mark.parametrize("change", ["text", "title", "source", "count", "order", "metadata"])
def test_refresh_refuses_changed_corpus(change):
    chunks = load_corpus()
    if change in {"text", "title", "source"}:
        chunks[0] = chunks[0].model_copy(update={change: "changed"})
    elif change == "count":
        chunks.pop()
    elif change == "order":
        chunks.reverse()
    else:
        chunks[0] = chunks[0].model_copy(update={"metadata": None})
    with pytest.raises(ValueError):
        refresh(old_index(), chunks)


def test_cli_refresh_does_not_construct_embedder(tmp_path, monkeypatch):
    from rag import __main__ as cli

    source, output = tmp_path / "old.json", tmp_path / "new.json"
    old_index().save(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("刷新不得访问嵌入服务")

    monkeypatch.setattr(cli, "OpenAIEmbedder", forbidden)
    cli.main(["refresh", "--from-index", str(source), "--out", str(output)])
    loaded = PolicyIndex.load(output)
    assert loaded.vectors == old_index().vectors
    assert all(c.metadata for c in loaded.chunks)


def test_cli_failure_preserves_existing_output(tmp_path):
    from rag import __main__ as cli

    source, output = tmp_path / "old.json", tmp_path / "new.json"
    old = old_index()
    old.chunks[0] = old.chunks[0].model_copy(update={"text": "changed"})
    old.save(source)
    output.write_text("original output", encoding="utf-8")
    with pytest.raises(ValueError):
        cli.main(["refresh", "--from-index", str(source), "--out", str(output)])
    assert output.read_text(encoding="utf-8") == "original output"
