import json

from agent_core.testing import make_client
from evals.cases import ALL_CASES
from evals.runner import Budget


async def test_baseline_budget_skip_preserves_planned_scope(tmp_path):
    from evals.baseline import run_baseline

    def forbidden(request):
        raise AssertionError("no requests after budget exhausted")

    results, path = await run_baseline(
        ALL_CASES[:2], client=make_client(forbidden), budget=Budget(0),
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    assert len(results) == 2
    assert all(r.failed_checks[0].startswith("budget:") for r in results)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["planned"] == 2
    assert payload["suite"]["executed"] == 0
    assert payload["suite"]["case_ids"] == [c.id for c in ALL_CASES[:2]]
    assert payload["suite"]["case_sha256"]


async def test_baseline_checkpoint_survives_later_setup_failure(tmp_path, monkeypatch):
    import pytest
    from evals import baseline
    from evals.model import CaseResult

    calls = 0

    async def first_ok_then_fail(case, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted before case completes")
        return CaseResult(case_id=case.id, category=case.category, passed=True), tmp_path

    monkeypatch.setattr(baseline, "run_case", first_ok_then_fail)
    with pytest.raises(RuntimeError):
        await baseline.run_baseline(
            ALL_CASES[:2], client=make_client(lambda request: None), budget=Budget(1),
            report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
        )
    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["results"][0]["passed"]
    assert payload["results"][1]["failed_checks"][0].startswith("not_run:")
    assert payload["suite"]["executed"] == 1 and not payload["suite"]["complete"]
    from evals.checks import SCORER_VERSION

    assert payload["suite"]["scorer_version"] == SCORER_VERSION
    assert payload["suite"]["seed"] == {
        "value": 20260930,
        "now": "2026-10-07T12:00:00+08:00",
        "fresh_database_per_case": True,
    }
    assert payload["suite"]["case_traces"][ALL_CASES[0].id] == tmp_path.resolve().as_posix()
