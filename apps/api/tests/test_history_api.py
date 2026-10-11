import sqlite3

import httpx2
from erpilot_api.main import create_app
from erpilot_api.run_store import RunStore
from langgraph.checkpoint.memory import InMemorySaver


def test_history_summary_paging_and_reopen(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    assert store.list_sessions() == {"items": [], "has_more": False}
    first = store.create_run("a", "首轮标题" + "长" * 100, [], "private-trace")
    store.finish_run(first, [{"role": "assistant", "content": "private-output"}], "private")
    second = store.create_run("a", "后续输入", [], None)
    store.fail_run(second, "private-error")
    pending = store.create_run("b", "待确认", [], None)
    store.project_run(pending, [], "waiting_approval")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE run_session SET updated_at='2026-10-11 00:00:00'")
    page = RunStore(path).list_sessions(limit=1)
    assert page == {
        "items": [
            {
                "session_id": "b",
                "title": "待确认",
                "updated_at": "2026-10-11T00:00:00Z",
                "status": "waiting_approval",
            }
        ],
        "has_more": True,
    }
    older = store.list_sessions(limit=1, offset=1)
    assert older["has_more"] is False
    assert older["items"][0]["title"] == ("首轮标题" + "长" * 100)[:80]
    assert older["items"][0]["status"] == "failed"
    assert set(older["items"][0]) == {"session_id", "title", "status", "updated_at"}
    assert store.list_sessions(offset=2)["items"] == []


async def test_http_history_is_read_only_and_validates_page(tmp_path):
    path = tmp_path / "runs.db"
    app = create_app(
        tools=[], trace_dir=tmp_path, run_store_path=path, checkpointer=InMemorySaver()
    )
    store = RunStore(path)
    run = store.create_run("pending", "等待确认", [], None)
    store.project_run(run, [], "waiting_approval")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/sessions?limit=1&offset=0")
        assert response.status_code == 200
        assert response.json()["items"][0]["session_id"] == "pending"
        for query in ("limit=0", "limit=51", "offset=-1"):
            assert (await client.get("/api/sessions?" + query)).status_code == 422
    assert store.get_run(run)["status"] == "waiting_approval"


async def test_default_application_has_empty_history(tmp_path):
    app = create_app(tools=[], trace_dir=tmp_path, checkpointer=InMemorySaver())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/sessions")
        assert response.status_code == 200
        assert response.json() == {"items": [], "has_more": False}
