from evals.checks import SCORER_VERSION
from evals.injection_support import (
    FIXED_EVALUATION_SEED,
    FIXED_EVALUATION_TIME,
    injection_suite_metadata,
    prompt_for_variant,
)
from evals.model import CaseCategory, CaseResult, EvalCase


def _case(case_id: str) -> EvalCase:
    return EvalCase(
        id=case_id, category=CaseCategory.ADVERSARIAL, question="查询状态", points="只读查询"
    )


def test_suite_metadata_records_reproducibility_and_redacts_endpoint_credentials() -> None:
    cases = [_case("inj-01"), _case("inj-02")]
    result = CaseResult(
        case_id="inj-01", category=CaseCategory.ADVERSARIAL, passed=True, completed=True
    )
    metadata = injection_suite_metadata(
        cases,
        [result],
        {"inj-01": "traces/inj-01.jsonl"},
        {"inj-01": {"before": {"stocks": []}, "after": {"stocks": []}}},
        prompt_variant="treatment",
        prompt_sha256="a" * 64,
        endpoint_label="https://alice:secret@example.test/v1/chat?q=token#fragment",
        timeout_seconds=30,
        budget_limit_cny=0.05,
    )

    assert metadata["kind"] == "targeted"
    assert metadata["case_ids"] == ["inj-01", "inj-02"]
    assert metadata["planned"] == 2 and metadata["executed"] == 1
    assert metadata["scorer_version"] == SCORER_VERSION
    assert len(metadata["case_sha256"]) == 64
    assert metadata["prompt_variant"] == "treatment"
    assert metadata["prompt_sha256"] == "a" * 64
    assert metadata["endpoint_label"] == "https://example.test/v1/chat"
    assert metadata["timeout_seconds"] == 30
    assert metadata["budget_limit_cny"] == 0.05
    assert metadata["runtime"] == "evals.runner.run_case"
    assert metadata["seed"] == {
        "value": FIXED_EVALUATION_SEED,
        "now": FIXED_EVALUATION_TIME,
        "fresh_database_per_case": True,
    }
    assert metadata["complete"] is False
    assert metadata["case_traces"] == {"inj-01": "traces/inj-01.jsonl"}
    assert "secret" not in str(metadata)
    assert "token" not in str(metadata)


def test_prompt_control_differs_only_by_shared_trust_boundary_rule() -> None:
    from agent_core.demo_tools import TOOL_DATA_TRUST_RULE, system_prompt

    control = prompt_for_variant(writes_enabled=True, variant="control")
    treatment = prompt_for_variant(writes_enabled=True, variant="treatment")
    assert control == system_prompt(True).replace(f"- {TOOL_DATA_TRUST_RULE}\n", "")
    assert treatment == system_prompt(True)
    assert TOOL_DATA_TRUST_RULE not in control


def test_unknown_prompt_variant_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="variant"):
        prompt_for_variant(writes_enabled=False, variant="old")  # type: ignore[arg-type]
