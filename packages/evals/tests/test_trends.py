import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from evals.trends import (
    build_trends,
    cohort_key,
    collect_inputs,
    main,
    render_markdown,
    render_svg,
    summarize_report,
    write_artifacts,
)


def _result(case_id, passed=True, *, failed=None, cost=0.001, complete=True, ms=100):
    return {
        "case_id": case_id,
        "passed": passed,
        "failed_checks": failed or [],
        "cost": cost,
        "cost_complete": complete,
        "duration_ms": ms,
    }


def _report(*, model="m1", ts="20261009-100000", suite=None, results=None, revision="abc"):
    return {
        "model": model,
        "ts": ts,
        "provenance": {"revision": revision, "source_sha256": "f" * 64},
        "suite": suite or {},
        "results": results if results is not None else [_result("a")],
    }


def _write(path: Path, data: dict):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_cohort_key_separates_prompt_scorer_cases_and_unknowns():
    first = _report(suite={"kind": "targeted", "scorer_version": "v2", "case_sha256": "a"})
    assert cohort_key(first) == ("m1", "v2", "targeted", "standard", "a")
    treatment = _report(suite={**first["suite"], "prompt_variant": "treatment"})
    assert cohort_key(treatment)[3] == "treatment"
    legacy = _report(suite={})
    assert cohort_key(legacy)[1:] == ("unknown", "unknown", "unknown", "unknown")
    assert cohort_key(_report(suite={"kind": "full", "case_ids": ["a", "b"]}))[4] != "unknown"


def test_summary_counts_executed_only_and_marks_cost_and_completion():
    report = _report(
        suite={"kind": "targeted", "planned": 4, "executed": 2, "complete": False},
        results=[
            _result("a", True, cost=0.002, ms=200),
            _result("b", False, cost=None, complete=False, ms=400),
            _result("c", False, failed=["budget: limit"], cost=99),
            _result("d", False, failed=["not_run: interrupted"], cost=99),
        ],
    )
    summary = summarize_report(report, source="run.json")
    assert summary["executed"] == 2
    assert summary["passed"] == 1
    assert summary["success_rate"] == 0.5
    assert summary["estimated_cost"] == 0.002
    assert summary["cost_complete"] is False
    assert summary["duration_ms"] == 600
    assert summary["mean_case_duration_ms"] == 300
    assert summary["incomplete_run"] is True
    empty = summarize_report(
        _report(results=[_result("z", False, failed=["budget: stop"])]), source="e.json"
    )
    assert empty["success_rate"] is None
    assert empty["cost_complete"] is False


def test_legacy_and_zero_execution_summaries_are_not_comparable():
    legacy = summarize_report(_report(suite={}, results=[]), source="legacy.json")
    assert legacy["comparable"] is False
    assert legacy["completion"] == "unknown"
    assert legacy["success_rate"] is None


def test_same_timestamp_observations_have_stable_source_order(tmp_path):
    _write(tmp_path / "z.json", _report(ts="same", results=[_result("z")]))
    _write(tmp_path / "a.json", _report(ts="same", results=[_result("a")]))
    trends = build_trends([tmp_path / "z.json", tmp_path / "a.json"])
    observations = trends["cohorts"][0]["observations"]
    assert [row["source"] for row in observations] == ["a.json", "z.json"]


def test_build_trends_keeps_different_same_time_cohorts_separate(tmp_path):
    _write(tmp_path / "a.json", _report(ts="same", suite={"kind": "full", "scorer_version": "v1"}))
    _write(
        tmp_path / "b.json", _report(ts="same", suite={"kind": "targeted", "scorer_version": "v1"})
    )
    assert len(build_trends(list(tmp_path.glob("*.json")))["cohorts"]) == 2


def test_renderers_show_unknown_partial_incomplete_cost_model_and_source(tmp_path):
    path = _write(
        tmp_path / "run.json",
        _report(
            suite={"planned": 2, "executed": 1, "complete": False},
            results=[
                _result("a", cost=None, complete=False),
                _result("b", False, failed=["budget: stop"]),
            ],
        ),
    )
    data = build_trends([path])
    markdown = render_markdown(data)
    svg = render_svg(data)
    assert "unknown" in markdown
    assert "m1" in markdown and "run.json" in markdown
    assert "不完整" in markdown and "1/2" in markdown and "incomplete" in markdown
    assert "<svg" in svg and "m1" in svg and "unknown" in svg
    ET.fromstring(svg)


def test_collect_inputs_excludes_generated_outputs_and_rejects_empty(tmp_path):
    _write(tmp_path / "run.json", _report())
    _write(tmp_path / "trends-v1.json", {"schema_version": 1, "cohorts": []})
    assert [p.name for p in collect_inputs([tmp_path])] == ["run.json"]
    with pytest.raises(ValueError, match="empty"):
        build_trends([])


def test_invalid_report_fails_before_outputs_are_written(tmp_path):
    invalid = tmp_path / "bad.json"
    invalid.write_text("{", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="bad.json"):
        write_artifacts([invalid], out)
    assert not list(out.glob("trends-v1.*")) if out.exists() else True


def test_stable_artifacts_have_no_timestamp_and_repeat_byte_identically(tmp_path):
    source = _write(tmp_path / "run.json", _report(suite={"kind": "full", "scorer_version": "v1"}))
    data = build_trends([source])
    assert render_markdown(data) == render_markdown(data)
    assert render_svg(data) == render_svg(data)
    assert "generated" not in render_markdown(data).casefold()
    first = write_artifacts([source], tmp_path / "out")
    first_bytes = {kind: path.read_bytes() for kind, path in first.items()}
    second = write_artifacts([source], tmp_path / "out")
    assert first_bytes == {kind: path.read_bytes() for kind, path in second.items()}


def test_completion_metadata_absence_stays_unknown_even_if_counts_match():
    report = _report(suite={"planned": 1, "executed": 1})
    summary = summarize_report(report, source="unknown-completion.json")
    assert summary["completion"] == "unknown"


def test_cli_accepts_explicit_file_and_rejects_empty_directory(tmp_path):
    source = _write(tmp_path / "one.json", _report())
    output = tmp_path / "created" / "outputs"
    assert main([str(source), "--output-dir", str(output)]) == 0
    assert all(path.exists() for path in output.glob("trends-v1.*"))
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(SystemExit):
        main([str(empty_dir), "--output-dir", str(tmp_path / "empty-output")])
    assert not (tmp_path / "empty-output").exists()
