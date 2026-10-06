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


def _write_intent(store, original_run_id, **changes):
    intent = dict(
        run_id=original_run_id, call_id="c1", session_id="s1", tool="adjust_stock",
        tool_schema_version="v1", arguments={"sku": "A1001", "delta": 3},
        client_token="token-1", pending_id="pending-1", ttl_seconds=1800,
    )
    intent.update(changes)
    return store.record_write_intent(**intent)


@pytest.mark.parametrize("change", [
    {"arguments": {"sku": "A1001", "delta": 99}},
    {"call_id": "c2"},
    {"tool": "cancel_order"},
    {"tool_schema_version": "v2"},
    {"pending_id": "pending-2"},
    {"session_id": "s2"},
    {"run_id": "other"},
])
def test_write_intent_replay_rejects_changed_identity(tmp_path, change):
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "调整库存", [], None)
    other_run = store.create_run("s2", "另一请求", [], None)
    invocation_id = _write_intent(store, run_id)
    before = store.get_invocation(invocation_id)
    approval = store.get_approval("pending-1")
    if "run_id" in change:
        change = {"run_id": other_run, "session_id": "s2"}
    with pytest.raises(ValueError):
        _write_intent(store, run_id, **change)
    assert store.get_invocation(invocation_id) == before
    assert store.get_approval("pending-1") == approval
    assert len(store.list_invocations(run_id)) == 1
    assert store.list_invocations(other_run) == []
    assert store.get_run(other_run)["status"] == "running"


def test_identical_write_intent_replay_preserves_decision_and_expiry(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "调整库存", [], None)
    invocation_id = _write_intent(store, run_id)
    store.record_approval_decision("pending-1", True)
    before = store.get_invocation(invocation_id)
    approval = store.get_approval("pending-1")
    assert _write_intent(
        RunStore(store.path), run_id, ttl_seconds=9999,
        arguments={"delta": 3, "sku": "A1001", "client_token": "ignored"},
    ) == invocation_id
    assert store.get_invocation(invocation_id) == before
    assert store.get_approval("pending-1") == approval


@pytest.mark.parametrize("status", [
    "prepared", "waiting_approval", "approved", "executing", "unknown",
])
def test_finish_preserves_unresolved_write_for_reconciliation(tmp_path, status):
    store = RunStore(tmp_path / "runs.db")
    history = [{"role": "system", "content": "rules"}]
    run_id = store.create_run("s1", "调整库存", history, None)
    invocation_id = _write_intent(store, run_id)
    store.set_invocation_status(invocation_id, status)
    messages = [*history, {"role": "assistant", "content": "结果待核对"}]
    store.finish_run(run_id, messages, "结果待核对")
    reopened = RunStore(store.path)
    assert reopened.get_run(run_id)["status"] == "recovering"
    assert reopened.get_run(run_id)["messages"] == messages
    assert reopened.get_run(run_id)["final_answer"] is None
    assert reopened.get_session("s1")["history"] == history
    assert reopened.get_session("s1")["active_run_id"] == run_id
    with pytest.raises(ActiveRunError):
        reopened.create_run("s1", "再次调整", history, None)


@pytest.mark.parametrize("status", ["unknown", "executing"])
def test_fail_preserves_uncertain_write_for_reconciliation(tmp_path, status):
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "调整库存", [], None)
    invocation_id = _write_intent(store, run_id)
    store.set_invocation_status(invocation_id, status)
    store.fail_run(run_id, "模型回答中断")
    assert store.get_run(run_id)["status"] == "recovering"
    assert store.get_run(run_id)["error"] == "模型回答中断"
    assert store.get_session("s1")["active_run_id"] == run_id


@pytest.mark.parametrize("status", ["succeeded", "denied", "failed"])
def test_finish_can_close_run_after_write_outcome_is_known(tmp_path, status):
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "调整库存", [], None)
    invocation_id = _write_intent(store, run_id)
    store.set_invocation_status(invocation_id, "unknown")
    assert store.finish_run(run_id, [], "待核对") is False
    store.set_invocation_status(invocation_id, status, {"outcome": status})
    messages = [{"role": "assistant", "content": "结果已核对"}]
    assert store.finish_run(run_id, messages, "结果已核对") is True
    assert store.get_run(run_id)["status"] == "completed"
    assert store.get_run(run_id)["final_answer"] == "结果已核对"
    assert store.get_session("s1")["history"] == messages
    assert store.get_session("s1")["active_run_id"] is None
    store.create_run("s1", "下一轮", messages, None)
