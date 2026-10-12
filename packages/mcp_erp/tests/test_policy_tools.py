from datetime import timedelta, timezone

import pytest
from erp_store.seed import seed_database
from fastmcp import Client
from mcp_erp.server import create_server
from rag.corpus import load_corpus
from rag.index import PolicyIndex


@pytest.fixture
def seeded_db(tmp_path):
    path = tmp_path / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    return path


class Embedder:
    def embed(self, texts):
        return [[1.0] for _ in texts]


async def test_search_policy_metadata(seeded_db):
    embedder = Embedder()
    index = PolicyIndex.build(load_corpus(), embedder)
    async with Client(
        create_server(seeded_db, policy_resolver=lambda: (index, embedder))
    ) as client:
        result = (await client.call_tool("search_policy", {"query": "政策"})).data
    assert result["matches"][0]["version"] == "2026-10-12.1"
    assert result["matches"][0]["effective_from"].endswith("Z")
    assert result["matches"][0]["policy_id"]


async def test_embedding_failure_is_sanitized(seeded_db):
    index = PolicyIndex.build(load_corpus(), Embedder())

    class Broken:
        def embed(self, texts):
            raise RuntimeError("secret-key-sensitive")

    async with Client(
        create_server(seeded_db, policy_resolver=lambda: (index, Broken()))
    ) as client:
        result = (await client.call_tool("search_policy", {"query": "政策"})).data
    assert result["error"]["code"] == "policy_unavailable"
    assert "secret-key" not in str(result)


async def test_reference_dates_are_normalized_to_utc(seeded_db):
    chunks = load_corpus()
    for chunk in chunks:
        chunk.metadata.effective_from = chunk.metadata.effective_from.astimezone(
            timezone(timedelta(hours=8))
        )
    embedder = Embedder()
    index = PolicyIndex.build(chunks, embedder)
    async with Client(
        create_server(seeded_db, policy_resolver=lambda: (index, embedder))
    ) as client:
        result = (await client.call_tool("search_policy", {"query": "政策"})).data
    assert result["matches"][0]["effective_from"] == "2026-10-11T16:00:00Z"
