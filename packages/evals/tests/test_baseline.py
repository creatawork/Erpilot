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



# ---- M06-03：run_baseline 的原币种预算路径 ----


def _stub_rate():
    from decimal import Decimal

    from evals.billing import EndpointRate

    return EndpointRate(
        model="deepseek-v4.1-flash",
        endpoint_host="test.local",
        currency="USD",
        input_per_m=Decimal("0.045"),
        output_per_m=Decimal("0.18"),
        price_source="test-fixture",
    )


class _AnyClient:
    from types import SimpleNamespace

    config = SimpleNamespace(model="deepseek-v4.1-flash")


async def test_run_baseline_currency_budget_reports_billing_not_cny(tmp_path, monkeypatch):
    from decimal import Decimal

    import evals.baseline as baseline_mod
    from evals.billing import BillingEstimate, BudgetedClient, CurrencyBudget
    from evals.model import CaseResult

    budget = CurrencyBudget(Decimal("0.05"), "USD", max_requests=40)

    async def fake_run_case(case, **kwargs):
        # 模拟 wrapper 在流结束时的记账：run_baseline 本身只读取预算对象汇总。
        estimate = BillingEstimate(
            currency="USD", estimated_amount="0.01",
            pricing_source="t", cache_assumption="uncached_upper_bound",
            complete=True,
        )
        budget.record(estimate)
        return (
            CaseResult(
                case_id=case.id, category=case.category, passed=True, cost=None,
                billing=estimate,
            ),
            tmp_path / "trace.jsonl",
        )

    monkeypatch.setattr(baseline_mod, "run_case", fake_run_case)
    client = BudgetedClient(_AnyClient(), _stub_rate(), budget)
    results, path = await baseline_mod.run_baseline(
        ALL_CASES[:1], client=client, budget=None, currency_budget=budget,
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["budget_limit_cny"] is None
    assert payload["suite"]["billing_budget"]["currency"] == "USD"
    assert Decimal(payload["suite"]["billing_budget"]["spent"]) == Decimal("0.01")
    assert payload["suite"]["billing_budget"]["requests"] == 0  # 记账发生在 wrapper 层
    assert payload["suite"]["billing_budget"]["unknown_usage"] is False
    assert payload["suite"]["retry_policy"] == "wrapper-no-rerun"
    assert payload["results"][0]["cost"] is None
    assert payload["results"][0]["billing"]["currency"] == "USD"
    markdown = path.read_text(encoding="utf-8")
    assert "$0.010000 USD" in markdown or "$0.01" in markdown
    assert "¥0.0100" not in markdown


async def test_run_baseline_currency_exhaust_marks_remaining_unrun(tmp_path, monkeypatch):
    from decimal import Decimal

    import evals.baseline as baseline_mod
    from evals.billing import BudgetedClient, CurrencyBudget

    async def forbidden_run_case(case, **kwargs):
        raise AssertionError("must not run a case after currency budget exhausted")

    monkeypatch.setattr(baseline_mod, "run_case", forbidden_run_case)
    budget = CurrencyBudget(Decimal("0"), "USD", max_requests=40)
    client = BudgetedClient(_AnyClient(), _stub_rate(), budget)
    results, path = await baseline_mod.run_baseline(
        ALL_CASES[:2], client=client, budget=None, currency_budget=budget,
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    assert len(results) == 2
    assert all(r.failed_checks[0].startswith("budget:") for r in results)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["billing_budget"]["currency"] == "USD"
    assert payload["suite"]["executed"] == 0
    assert payload["suite"]["budget_limit_cny"] is None
