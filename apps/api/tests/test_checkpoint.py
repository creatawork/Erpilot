import os
from uuid import uuid4

import pytest
import pytest_asyncio
from erpilot_api.checkpoint import open_checkpointer, thread_id_for_session


def test_thread_id_is_stable_and_bounded():
    session_id = "会话/🛠" * 10000
    first = thread_id_for_session(session_id)
    assert first == thread_id_for_session(session_id)
    assert len(first) <= 100
    assert first.isascii()
    assert first != thread_id_for_session(session_id + "x")


async def test_missing_checkpoint_database_url_fails_closed():
    with pytest.raises(RuntimeError, match="ERPILOT_CHECKPOINT_DATABASE_URL"):
        async with open_checkpointer(""):
            pytest.fail("missing database must not yield a saver")


async def test_request_error_is_not_reported_as_checkpoint_startup_failure(monkeypatch):
    from contextlib import asynccontextmanager

    from erpilot_api import checkpoint

    class Saver:
        async def setup(self):
            pass

    @asynccontextmanager
    async def saver_context(*args, **kwargs):
        yield Saver()

    monkeypatch.setattr(
        checkpoint.AsyncPostgresSaver, "from_conn_string", saver_context
    )
    with pytest.raises(ValueError, match="request failed"):
        async with checkpoint.open_checkpointer("postgresql://unused"):
            raise ValueError("request failed")


async def test_api_checkpoint_failure_is_fail_closed(monkeypatch, tmp_path):
    from erpilot_api.main import create_app

    monkeypatch.delenv("ERPILOT_CHECKPOINT_DATABASE_URL", raising=False)
    app = create_app(tools=[], trace_dir=tmp_path)
    with pytest.raises(RuntimeError, match="ERPILOT_CHECKPOINT_DATABASE_URL"):
        async with app.router.lifespan_context(app):
            pytest.fail("API should not start without checkpoint configuration")


async def test_api_accepts_explicit_test_saver(tmp_path):
    from erpilot_api.main import create_app
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    app = create_app(tools=[], trace_dir=tmp_path, checkpointer=saver)
    async with app.router.lifespan_context(app):
        assert app.state.service._checkpointer is saver


@pytest_asyncio.fixture
async def postgres_url():
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    url = os.environ.get("ERPILOT_TEST_CHECKPOINT_DATABASE_URL")
    if not url:
        pytest.skip("requires explicit isolated PostgreSQL test configuration")
    schema = "test_checkpoint_" + uuid4().hex
    async with await AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(url, options=f"-csearch_path={schema}")
    finally:
        async with await AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("approved", [False, True])
async def test_postgres_interrupt_survives_runtime_and_saver_reconstruction(postgres_url, approved):
    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.llm import StreamEnd, TextDelta, ToolCall
    from agent_core.tools import Tool
    from pydantic import BaseModel

    class Params(BaseModel):
        value: int
        client_token: str | None = None

    class Client:
        async def stream_chat(self, messages, tools=None):
            if messages and messages[-1]["role"] == "tool":
                yield TextDelta("done")
            else:
                yield ToolCall("write_id", "write", '{"value":7}')
            yield StreamEnd()

    executed = []

    async def handler(args):
        executed.append(args.model_dump())
        return {"value": args.value}

    tools = [Tool("write", "write", Params, handler, risk="confirm", retry_safe=True)]
    thread = thread_id_for_session("postgres-recovery")
    async with open_checkpointer(postgres_url) as saver:
        runtime = LangGraphRuntime(Client(), tools, checkpointer=saver, approval_enabled=True)
        events = [e async for e in runtime.stream([], thread_id=thread)]
        pending = events[-1]
        original = pending.arguments.copy()
    assert not executed
    async with open_checkpointer(postgres_url) as saver:
        runtime = LangGraphRuntime(Client(), tools, checkpointer=saver, approval_enabled=True)
        snapshot = await runtime.get_state(thread)
        payload = snapshot.tasks[0].interrupts[0].value
        assert payload["arguments"] == original
        assert payload["pending_id"] == pending.pending_id
        events = [
            e
            async for e in runtime.resume(
                thread,
                {
                    "pending_id": pending.pending_id,
                    "approved": approved,
                },
            )
        ]
        assert events[-1].completed
    assert executed == ([original] if approved else [])
