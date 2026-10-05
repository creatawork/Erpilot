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
    import json

    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["provenance"]["revision"]
    assert payload["provenance"]["source_sha256"]
    assert isinstance(payload["provenance"]["working_tree_dirty"], bool)
