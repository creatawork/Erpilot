import json

from evals.model import CaseCategory, CaseResult
from evals.report import render_report, write_report


def test_report_exposes_partial_cost_and_retries(tmp_path):
    results = [CaseResult(
        case_id="t-01", category=CaseCategory.SINGLE, passed=True,
        cost=0.002, cost_complete=False, attempts=2,
    )]
    report = render_report(results, model="test")
    assert "计量不完整" in report
    assert "重试次数" in report
    path = write_report(tmp_path, results, model="test")
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["provenance"]["revision"]
    assert payload["provenance"]["source_sha256"]
    assert isinstance(payload["provenance"]["working_tree_dirty"], bool)


# ---- M06-03：报告币种契约（USD 用 $，绝不冒充 ¥；未计量如实显示） ----


def _usd_billed_result() -> "CaseResult":
    from evals.billing import BillingEstimate

    return CaseResult(
        case_id="b5-1", category=CaseCategory.SINGLE, passed=True,
        cost=None, cost_complete=False,
        billing=BillingEstimate(
            currency="USD", estimated_amount="0.225",
            pricing_source="user-confirmed-2026-10-12",
            cache_assumption="uncached_upper_bound", complete=True,
        ),
    )


def test_report_shows_usd_billing_and_keeps_cny_out(tmp_path):
    billing_budget = {"currency": "USD", "limit": "0.10", "spent": "0.225",
                      "requests": 2, "unknown_usage": False}
    path = write_report(
        tmp_path, [_usd_billed_result()], model="deepseek-v4.1-flash",
        metadata={"billing_budget": billing_budget, "retry_policy": "wrapper-no-rerun"},
    )
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["billing_budget"]["currency"] == "USD"
    assert payload["suite"]["billing_budget"]["limit"] == "0.10"
    assert payload["results"][0]["cost"] is None
    assert payload["results"][0]["billing"]["currency"] == "USD"
    markdown = path.read_text(encoding="utf-8")
    assert "$0.225" in markdown
    assert "¥0.225" not in markdown
    assert "预算上限 $0.10 USD" in markdown
    assert "wrapper-no-rerun" in markdown


def test_report_marks_unmeasured_without_zero_fee_look(tmp_path):
    results = [CaseResult(case_id="t-02", category=CaseCategory.SINGLE, passed=True)]
    report = render_report(results, model="test")
    assert "未计量" in report


def test_report_keeps_legacy_cny_contract(tmp_path):
    results = [CaseResult(case_id="t-03", category=CaseCategory.SINGLE, passed=True,
                          cost=0.002)]
    path = write_report(tmp_path, results, model="glm-5.3-flash", budget_limit_cny=0.02)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert "billing_budget" not in payload["suite"]
    markdown = path.read_text(encoding="utf-8")
    assert "≈¥0.0020" in markdown
    assert "预算上限 ¥0.02" in markdown
