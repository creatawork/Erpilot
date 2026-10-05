"""T04: read-only run checkpoints survive construction of a new service."""

import pytest
from erpilot_api.run_store import ActiveRunError, RunStore, SchemaVersionError


def test_completed_run_and_history_survive_reopen(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    before = [{"role": "system", "content": "rules"}]
    run_id = store.create_run("s1", "查订单", before, "trace.jsonl")
    messages = [
        *before,
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "get_order", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "已发货"},
    ]
    store.finish_run(run_id, messages, "已发货")

    reopened = RunStore(path)
    assert reopened.get_run(run_id)["messages"] == messages
    assert reopened.get_run(run_id)["status"] == "completed"
    assert reopened.get_run(run_id)["trace_path"] == "trace.jsonl"
    assert reopened.get_session("s1")["history"] == messages


def test_only_one_active_run_per_session_across_store_instances(tmp_path) -> None:
    path = tmp_path / "runs.db"
    first = RunStore(path)
    first.create_run("s1", "one", [], None)
    with pytest.raises(ActiveRunError):
        RunStore(path).create_run("s1", "two", [], None)


def test_unknown_schema_is_not_overwritten(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_run("s1", "one", [], None)
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE run_session SET schema_version=999 WHERE session_id='s1'")
    with pytest.raises(SchemaVersionError):
        RunStore(path).get_session("s1")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT schema_version FROM run_session").fetchone()[0] == 999


def test_failed_run_preserves_previous_history(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    history = [{"role": "system", "content": "rules"}]
    run_id = store.create_run("s1", "bad", history, None)
    store.fail_run(run_id, "upstream failed")
    assert store.get_run(run_id)["status"] == "failed"
    assert store.get_session("s1")["history"] == history
