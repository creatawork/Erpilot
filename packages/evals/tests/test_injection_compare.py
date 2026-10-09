import json

import pytest
from evals.injection_compare import compare_injection_reports, main, write_comparison


def _report(variant):
    cases = ["inj-03", "inj-04"]
    return {
        "model": "model-x",
        "ts": "20261009-120000",
        "provenance": {"revision": "rev-1", "source_sha256": "a" * 64},
        "suite": {
            "kind": "targeted",
            "case_ids": cases,
            "case_sha256": "b" * 64,
            "scorer_version": "v2.4",
            "prompt_variant": variant,
            "prompt_sha256": "c" * 64 if variant == "control" else "d" * 64,
            "seed": {"value": 20260930, "now": "fixed", "fresh_database_per_case": True},
            "timeout_seconds": 30,
            "budget_limit_cny": 0.05,
            "runtime": "evals.runner.run_case",
            "endpoint_label": "https://example.test/v1",
            "planned": 2,
            "executed": 2,
            "complete": True,
            "source_changed_during_run": False,
            "case_traces": {case: f"traces/{variant}-{case}.jsonl" for case in cases},
            "case_snapshots": {
                case: {
                    "before": {"stocks": [{"quantity": 0}]},
                    "after": {"stocks": [{"quantity": 0}]},
                }
                for case in cases
            },
        },
        "results": [
            {
                "case_id": case,
                "passed": True,
                "failed_checks": [],
                "tool_calls": ["get_stock"],
                "cost": 0.001,
                "cost_complete": True,
                "total_tokens": 100,
                "duration_ms": 200,
                "tool_results": [],
            }
            for case in cases
        ],
    }


def test_compare_preserves_arm_results_and_computes_separate_deltas():
    control = _report("control")
    treatment = _report("treatment")
    treatment["results"][1]["passed"] = False
    treatment["results"][1]["failed_checks"] = ["must_not_mention: 999"]
    result = compare_injection_reports(control, treatment)
    assert result["comparison_type"] == "controlled_prompt_ablation"
    assert result["control"]["success_rate"] == 1
    assert result["treatment"]["success_rate"] == 0.5
    assert result["deltas"]["success_rate"] == -0.5
    assert [row["case_id"] for row in result["cases"]] == ["inj-03", "inj-04"]
    assert result["cases"][1]["treatment"]["failed_checks"] == ["must_not_mention: 999"]
    assert result["cases"][0]["control"]["trace"] == "traces/control-inj-03.jsonl"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model",), "other"),
        (("provenance", "revision"), "other-rev"),
        (("provenance", "source_sha256"), "e" * 64),
        (("suite", "scorer_version"), "v3"),
        (("suite", "case_sha256"), "other-cases"),
        (("suite", "seed", "value"), 9),
        (("suite", "seed", "now"), "other-time"),
        (("suite", "budget_limit_cny"), 0.1),
        (("suite", "timeout_seconds"), 45),
        (("suite", "runtime"), "different-runtime"),
        (("suite", "endpoint_label"), "https://other.test"),
    ],
)
def test_compare_rejects_mismatched_controls(path, value):
    control, treatment = _report("control"), _report("treatment")
    target = treatment
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="mismatch"):
        compare_injection_reports(control, treatment)


@pytest.mark.parametrize("variant", ["control", "treatment"])
def test_compare_requires_correct_variants_and_distinct_prompts(variant):
    control, treatment = _report("control"), _report("treatment")
    (control if variant == "control" else treatment)["suite"]["prompt_variant"] = "wrong"
    with pytest.raises(ValueError, match="prompt_variant"):
        compare_injection_reports(control, treatment)
    control, treatment = _report("control"), _report("treatment")
    treatment["suite"]["prompt_sha256"] = control["suite"]["prompt_sha256"]
    with pytest.raises(ValueError, match="prompt"):
        compare_injection_reports(control, treatment)


def test_compare_rejects_missing_duplicate_cases_and_changed_state():
    control, treatment = _report("control"), _report("treatment")
    treatment["results"].pop()
    with pytest.raises(ValueError, match="case_ids"):
        compare_injection_reports(control, treatment)
    control, treatment = _report("control"), _report("treatment")
    treatment["results"][1]["case_id"] = "inj-03"
    with pytest.raises(ValueError, match="duplicate"):
        compare_injection_reports(control, treatment)
    control, treatment = _report("control"), _report("treatment")
    treatment["suite"]["case_snapshots"]["inj-04"]["after"]["stocks"][0]["quantity"] = 3
    with pytest.raises(ValueError, match="state changed"):
        compare_injection_reports(control, treatment)


def test_compare_rejects_endpoint_credentials_and_query_values():
    control, treatment = _report("control"), _report("treatment")
    endpoint = "https://user:secret@example.test/v1?key=private"
    control["suite"]["endpoint_label"] = endpoint
    treatment["suite"]["endpoint_label"] = endpoint
    with pytest.raises(ValueError, match="endpoint_label"):
        compare_injection_reports(control, treatment)


def test_compare_requires_source_stability_and_snapshots_for_executed_cases():
    control, treatment = _report("control"), _report("treatment")
    control["suite"]["source_changed_during_run"] = True
    with pytest.raises(ValueError, match="source_changed_during_run"):
        compare_injection_reports(control, treatment)
    control, treatment = _report("control"), _report("treatment")
    del treatment["suite"]["case_snapshots"]["inj-04"]
    with pytest.raises(ValueError, match="snapshot missing"):
        compare_injection_reports(control, treatment)


def test_compare_retains_failed_and_skipped_outcomes_without_pooling():
    control, treatment = _report("control"), _report("treatment")
    control["results"][0]["failed_checks"] = ["budget: cap"]
    control["results"][0]["passed"] = False
    treatment["results"][1]["passed"] = False
    treatment["results"][1]["failed_checks"] = ["tool error"]
    result = compare_injection_reports(control, treatment)
    assert result["control"]["executed"] == 1
    assert result["treatment"]["executed"] == 2
    assert result["cases"][0]["control"]["status"] == "skipped"
    assert result["cases"][1]["treatment"]["status"] == "failed"


def test_write_comparison_creates_json_and_markdown(tmp_path):
    outputs = write_comparison(_report("control"), _report("treatment"), tmp_path)
    payload = json.loads(outputs["json"].read_text(encoding="utf-8"))
    assert payload["comparison_type"] == "controlled_prompt_ablation"
    markdown = outputs["markdown"].read_text(encoding="utf-8")
    assert "controlled prompt ablation" in markdown
    assert "inj-04" in markdown


def test_comparison_cli_reads_reports_without_running_models(tmp_path):
    control_path = tmp_path / "control.json"
    treatment_path = tmp_path / "treatment.json"
    control_path.write_text(json.dumps(_report("control")), encoding="utf-8")
    treatment_path.write_text(json.dumps(_report("treatment")), encoding="utf-8")
    output = tmp_path / "security"
    assert main([str(control_path), str(treatment_path), "--output-dir", str(output)]) == 0
    assert len(list(output.glob("*-prompt-injection-comparison.json"))) == 1
