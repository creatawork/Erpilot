"""API-owned PostgreSQL checkpoint lifecycle; ERP data remains in SQLite."""

from contextlib import AsyncExitStack, asynccontextmanager
from hashlib import sha256

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool


def thread_id_for_session(session_id: str) -> str:
    return "erpilot:" + sha256(session_id.encode("utf-8")).hexdigest()


@asynccontextmanager
async def open_checkpointer(database_url: str):
    if not database_url:
        raise RuntimeError("缺少 ERPILOT_CHECKPOINT_DATABASE_URL：API 需要 PostgreSQL checkpoints")
    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    stack = AsyncExitStack()
    try:
        await stack.enter_async_context(pool)
        saver = AsyncPostgresSaver(
            pool,
            serde=JsonPlusSerializer(allowed_msgpack_modules=[]),
        )
        await saver.setup()
    except Exception as exc:
        await stack.aclose()
        # Connection strings may contain passwords: never include them in startup errors.
        raise RuntimeError("PostgreSQL checkpoint 初始化或连接失败") from exc
    try:
        yield saver
    finally:
        await stack.aclose()
