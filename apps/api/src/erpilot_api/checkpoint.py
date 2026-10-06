"""API-owned PostgreSQL checkpoint lifecycle; ERP data remains in SQLite."""

from contextlib import AsyncExitStack, asynccontextmanager
from hashlib import sha256

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def thread_id_for_session(session_id: str) -> str:
    return "erpilot:" + sha256(session_id.encode("utf-8")).hexdigest()


@asynccontextmanager
async def open_checkpointer(database_url: str):
    if not database_url:
        raise RuntimeError("缺少 ERPILOT_CHECKPOINT_DATABASE_URL：API 需要 PostgreSQL checkpoints")
    stack = AsyncExitStack()
    try:
        saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(
            database_url,
            serde=JsonPlusSerializer(allowed_msgpack_modules=[]),
        ))
        await saver.setup()
    except Exception as exc:
        await stack.aclose()
        # Connection strings may contain passwords: never include them in startup errors.
        raise RuntimeError("PostgreSQL checkpoint 初始化或连接失败") from exc
    try:
        yield saver
    finally:
        await stack.aclose()
