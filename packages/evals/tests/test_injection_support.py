from erp_store.models import OrderStatus
from evals.checks import SCORER_VERSION
from evals.injection_support import (
    FIXED_EVALUATION_SEED,
    FIXED_EVALUATION_TIME,
    apply_injection_harness_checks,
    injection_suite_metadata,
    make_timeout_result,
    prompt_for_variant,
    resolve_injection_context,
)
from evals.model import CaseCategory, CaseResult, EvalCase, ToolResult


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


def test_injection_harness_failure_is_recorded_before_report_checkpoint() -> None:
    result = CaseResult(
        case_id="inj-02", category=CaseCategory.ADVERSARIAL, passed=True, completed=True
    )
    failures = apply_injection_harness_checks(
        result,
        "inj-02",
        approval_requests=[{"tool": "adjust_stock", "arguments": {"sku": "A1001", "delta": 999}}],
        before={"stock": 10},
        after={"stock": 10},
        tool_results=[],
    )

    assert failures
    assert result.passed is False
    assert result.failed_checks == failures


def test_injection_harness_records_successful_write_and_state_mutation() -> None:
    result = CaseResult(
        case_id="inj-02", category=CaseCategory.ADVERSARIAL, passed=True, completed=True
    )
    failures = apply_injection_harness_checks(
        result,
        "inj-02",
        approval_requests=[{"tool": "adjust_stock", "arguments": {"sku": "A1001", "delta": 1}}],
        before={"stock": 10},
        after={"stock": 11},
        tool_results=[
            ToolResult(
                call_id="call-1",
                name="adjust_stock",
                arguments={"sku": "A1001", "delta": 1},
                content={"quantity": 11},
                ok=True,
            )
        ],
    )

    assert any("state_changed" in failure for failure in failures)
    assert any("write_denial" in failure for failure in failures)
    assert result.passed is False


def test_expected_capture_and_deny_harness_checks_pass() -> None:
    result = CaseResult(
        case_id="inj-02", category=CaseCategory.ADVERSARIAL, passed=True, completed=True
    )
    failures = apply_injection_harness_checks(
        result,
        "inj-02",
        approval_requests=[{"tool": "adjust_stock", "arguments": {"sku": "A1001", "delta": 1}}],
        before={"stock": 10},
        after={"stock": 10},
        tool_results=[
            ToolResult(
                call_id="call-1",
                name="adjust_stock",
                arguments={"sku": "A1001", "delta": 1},
                content={"approval": "denied"},
                ok=True,
            )
        ],
    )

    assert failures == []
    assert result.passed is True


def test_injection_context_resolves_only_the_placeholder_used_by_these_cases() -> None:
    class Repository:
        def list_orders(self, *, status, limit):
            assert status is OrderStatus.PENDING_SHIPMENT
            assert limit == 1
            return [type("Order", (), {"order_id": "ORD-2026-0001"})()]

        def __getattr__(self, name):
            raise AssertionError(f"unexpected dependency on {name}")

    assert resolve_injection_context(Repository()) == {"order_id": "ORD-2026-0001"}


def test_timeout_result_records_elapsed_duration() -> None:
    case = _case("inj-01")
    result = make_timeout_result(case, elapsed_seconds=2.75)

    assert result.passed is False
    assert result.duration_ms == 2750
    assert result.cost_complete is False
    assert result.failed_checks == ["timeout: case exceeded 30 seconds"]


async def test_real_guard_denial_passes_injection_harness(tmp_path) -> None:
    import json

    from agent_core.approval import AutoDenyGate
    from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
    from erp_store.db import make_engine
    from erp_store.seed import seed_database
    from evals.prompt_injection_cases import PROMPT_INJECTION_CASES
    from evals.runner import run_case
    from evals.state import snapshot
    from mcp_erp import build_agent_tools_async

    def handler(request):
        body = json.loads(request.content)
        count = sum(message["role"] == "tool" for message in body["messages"])
        if count == 0:
            return sse_response(tool_call_chunks("read", "get_stock", '{"sku":"A1001"}'))
        if count == 1:
            return sse_response(
                tool_call_chunks("write", "adjust_stock", '{"sku":"A1001","delta":1}')
            )
        return sse_response([chunk(delta={"content": "需要审批，操作未执行。"})])

    class CaptureGate(AutoDenyGate):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def review(self, request):
            self.requests.append({"tool": request.tool, "arguments": request.arguments})
            return await super().review(request)

    db = tmp_path / "erp.sqlite"
    seed_database(db)
    engine = make_engine(db)
    gate = CaptureGate()
    try:
        before = snapshot(engine)
        result, _ = await run_case(
            PROMPT_INJECTION_CASES[1],
            client=make_client(handler),
            tools=await build_agent_tools_async(db, writes=True, approval_gate=gate),
            resolved={},
            trace_dir=tmp_path / "traces",
            state_engine=engine,
            system_prompt=prompt_for_variant(writes_enabled=True, variant="treatment"),
        )
        assert result.passed, result.failed_checks
        assert result.tool_results[-1].ok is True
        failures = apply_injection_harness_checks(
            result,
            "inj-02",
            approval_requests=gate.requests,
            before=before,
            after=snapshot(engine),
            tool_results=result.tool_results,
        )
        assert failures == [], gate.requests
        assert result.passed
    finally:
        engine.dispose()
