from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from erpilot_api.run_store import ApprovalStateError, RunStore


def intent(store, ttl=1800):
    run = store.create_run("s", "adjust", [], None)
    inv = store.record_write_intent(
        run, "c", session_id="s", tool="adjust_stock", tool_schema_version="v1",
        arguments={"sku": "A1001", "delta": 3}, client_token="tok", pending_id="p",
        ttl_seconds=ttl,
    )
    return run, inv


def test_durable_decision_repeats_and_conflicts_across_restart(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run, inv = intent(store)
    first = store.decide_approval("p", True, "yes", run_id=run, session_id="s",
                                  expected_version=1)
    reopened = RunStore(store.path)
    assert reopened.decide_approval("p", True, "different", expected_version=1) == first
    assert reopened.get_invocation(inv)["status"] == "approved"
    with pytest.raises(ApprovalStateError) as error:
        reopened.decide_approval("p", False, expected_version=1)
    assert error.value.status_code == 409
    assert reopened.get_approval("p") == first


def test_expiry_is_absolute_and_only_applies_before_decision(tmp_path):
    now = datetime(2026, 10, 6, tzinfo=UTC)
    store = RunStore(tmp_path / "runs.db", clock=lambda: now)
    intent(store, ttl=30)
    now += timedelta(seconds=30)
    with pytest.raises(ApprovalStateError) as error:
        RunStore(store.path, clock=lambda: now).decide_approval("p", True)
    assert error.value.status_code == 410
    assert store.get_approval("p")["status"] == "expired"
    assert store.list_pending_approvals(store.get_approval("p")["run_id"]) == []


@pytest.mark.parametrize("kwargs", [
    {"run_id": "wrong"}, {"session_id": "wrong"}, {"expected_version": 9},
    {"arguments_fingerprint": "wrong"},
])
def test_wrong_approval_identity_has_no_effect(tmp_path, kwargs):
    store = RunStore(tmp_path / "runs.db")
    _, inv = intent(store)
    with pytest.raises(ApprovalStateError) as error:
        store.decide_approval("p", True, **kwargs)
    assert error.value.status_code == 409
    assert store.get_invocation(inv)["status"] == "waiting_approval"


def test_competing_decisions_have_one_persistent_winner(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    intent(store)
    def decide(approved):
        try:
            return RunStore(store.path).decide_approval("p", approved)
        except ApprovalStateError as exc:
            return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(decide, [True, False]))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert 409 in outcomes
    assert store.get_approval("p")["status"] in ("approved", "denied")


def test_cancel_invalidates_pending_but_preserves_committed_result(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run, inv = intent(store)
    store.cancel_run(run)
    with pytest.raises(ApprovalStateError) as error:
        store.decide_approval("p", True)
    assert error.value.status_code == 409
    assert store.get_invocation(inv)["status"] == "denied"
    assert store.get_run(run)["status"] == "cancelled"
