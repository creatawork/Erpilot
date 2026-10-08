"""T04: read-only run checkpoints survive construction of a new service."""

import sqlite3

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


def _event(run_id, seq, *, event_id=None):
    return {
        "event_id": event_id or f"e{seq}",
        "session_id": "s1",
        "run_id": run_id,
        "segment_id": "segment",
        "seq": seq,
        "type": "delta",
        "payload": {"text": str(seq)},
    }


def test_presentation_events_have_unique_sequences_and_cursor_replay(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "hello", [], None)
    store.append_presentation_events([_event(run_id, 1), _event(run_id, 2)])

    assert [event["seq"] for event in store.read_events(run_id, after_seq=1)] == [2]
    assert [event["seq"] for event in store.read_events(run_id)] == [1, 2]
    assert store.last_seq(run_id) == 2
    assert store.append_presentation_events([_event(run_id, 2, event_id="new-event")]) == 3
    assert store.append_presentation_events([_event(run_id, 99, event_id="e2")]) == 3
    assert [event["seq"] for event in store.read_events(run_id)] == [1, 2, 3]


def test_sequence_migration_refuses_duplicate_legacy_rows_without_deleting_them(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    run_id = store.create_run("s1", "hello", [], None)
    store.append_presentation_events([_event(run_id, 1)])
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX presentation_by_run_seq")
        conn.execute(
            "INSERT INTO presentation_event "
            "(event_id, session_id, run_id, segment_id, seq, type, payload, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-duplicate", "s1", run_id, "old", 1, "delta", '{"text":"old"}', 1),
        )

    with pytest.raises(SchemaVersionError, match="duplicate historical presentation sequence"):
        RunStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM presentation_event WHERE run_id=? AND seq=1", (run_id,)
        ).fetchone()[0] == 2


def test_pruning_only_deletes_old_terminal_run_events(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    active = store.create_run("s1", "active", [], None)
    store.append_presentation_events([_event(active, 1)])
    store.project_run(active, [], "unknown")
    done = store.create_run("s2", "done", [], None)
    store.append_presentation_events([_event(done, 1)])
    store.finish_run(done, [], "done")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE presentation_event SET occurred_at='2000-01-01'")

    store.prune_presentation()

    assert len(store.read_events(active)) == 1
    assert store.read_events(done) == []
