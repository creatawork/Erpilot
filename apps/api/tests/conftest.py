import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio


def pytest_asyncio_loop_factories(config, item):
    # psycopg async requires selectors on Windows; a single factory also works on Linux.
    return {"selector": asyncio.SelectorEventLoop}


@pytest_asyncio.fixture
async def isolated_postgres_url():
    """One disposable schema per hard-crash test; never targets a developer DB."""
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    url = os.environ.get("ERPILOT_TEST_CHECKPOINT_DATABASE_URL")
    if not url:
        pytest.skip("requires explicit isolated PostgreSQL test configuration")
    schema = "test_process_recovery_" + uuid4().hex
    async with await AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(url, options=f"-csearch_path={schema}")
    finally:
        async with await AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
