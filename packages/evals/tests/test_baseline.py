import json

import pytest
from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
from evals.cases import ALL_CASES
from evals.runner import Budget


def test_select_cases_keeps_write_errors_out_of_original_baseline():
    from evals.baseline import select_cases

    baseline = select_cases("baseline")
    errors = select_cases("write-errors")
    assert len(baseline) == 35
    assert {c.id for c in baseline}.isdisjoint({"adv-24", "adv-25"})
    assert [c.id for c in errors] == ["adv-24", "adv-25"]
    assert [c.id for c in select_cases("write-errors", ["adv-25"])] == ["adv-25"]
    with pytest.raises(ValueError, match="adv-24"):
        select_cases("baseline", ["adv-24"])


def test_write_preflight_suite_isolated_from_business_error_observations():
    from evals.baseline import SUITES, select_cases

    preflight = select_cases("write-preflight")
    assert [c.id for c in preflight] == ["adv-26", "adv-27"]
    assert all(c.state and c.state.kind == "unchanged" for c in preflight)
    assert all(not c.expect_error_codes for c in preflight)
    assert set(SUITES["write-errors"][0].expect_error_codes) == {"insufficient_stock"}
    assert [c.id for c in select_cases("baseline")] == [c.id for c in SUITES["baseline"]]


async def test_write_preflight_suite_queries_state_without_mutating(tmp_path):
    from evals.baseline import run_baseline
    from evals.write_cases import WRITE_PREFLIGHT_CASES

    def handler(request):
        body = json.loads(request.content)
        question = next(m["content"] for m in body["messages"] if m["role"] == "user")
        tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
        if tool_messages:
            response = (
                "目前库存为 0 件，无法出库。"
                if "出库" in question else "目前已在售，无需上架。"
            )
            return sse_response([chunk(delta={"content": response})])
        tool = "get_stock" if "出库" in question else "get_product"
        arguments = {"sku": question.split()[1]}
        return sse_response(tool_call_chunks("read_1", tool, json.dumps(arguments)))

    results, path = await run_baseline(
        WRITE_PREFLIGHT_CASES, client=make_client(handler), budget=Budget(1),
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    assert all(r.passed for r in results), [r.failed_checks for r in results]
    assert [r.tool_calls for r in results] == [["get_stock"], ["get_product"]]
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["name"] == "write-preflight"
    assert payload["suite"]["state_checks"] == 2
    for case_id in ("adv-26", "adv-27"):
        evidence_path = path.with_suffix("") / f"{case_id}.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["state_before"] == evidence["state_after"]
        assert evidence["approval_gate"] == "AutoDenyGate"
        assert evidence["approval_decisions"] == []


async def test_write_error_suite_reaches_business_errors_and_records_isolated_state(tmp_path):
    from evals.baseline import run_baseline
    from evals.write_cases import WRITE_ERROR_CASES

    def handler(request):
        body = json.loads(request.content)
        question = next(m["content"] for m in body["messages"] if m["role"] == "user")
        error_case = "出库 5 件" in question
        tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
        sku = question.split()[1]
        if tool_messages:
            return sse_response([
                chunk(delta={
                    "content": "库存不足，无法出库" if error_case else "已在售，无需变更",
                }),
                chunk(usage={"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}),
            ])
        name = "adjust_stock" if error_case else "set_product_status"
        arguments = {"sku": sku, "delta": -5} if error_case else {"sku": sku, "status": "在售"}
        return sse_response(tool_call_chunks("write_1", name, json.dumps(arguments)))

    results, path = await run_baseline(
        WRITE_ERROR_CASES, client=make_client(handler, max_retries=0), budget=Budget(1),
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    assert all(r.passed for r in results), [r.failed_checks for r in results]
    assert [r.tool_results[0].content["error"]["code"] for r in results] == [
        "insufficient_stock", "invalid_transition",
    ]
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["suite"]["name"] == "write-errors"
    assert payload["suite"]["kind"] == "full"
    assert payload["suite"]["planned"] == payload["suite"]["executed"] == 2
    assert payload["suite"]["state_checks"] == 2
    evidence = [json.loads((path.parent / item).read_text(encoding="utf-8"))
                for item in payload["suite"]["evidence"].values()]
    assert {item["database"] for item in evidence} == {"adv-24.db", "adv-25.db"}
    for item in evidence:
        assert item["state_before"] == item["state_after"]
        assert set(item["state_before"]) == {"products", "stocks", "orders", "order_items"}
        assert item["approval_gate"] == "ScriptedPolicyGate"
        assert item["approval_decisions"][0]["decision"]["approved"]
        assert item["approval_evidence"] == "scripted_policy"
        assert item["human_approval"] is False
        assert item["trace_path"] and item["result"]["attempts"] == 1


async def test_prior_approved_case_cannot_change_write_error_seed(tmp_path):
    from evals.approval_cases import APPROVAL_CASES
    from evals.baseline import run_baseline
    from evals.write_cases import WRITE_ERROR_CASES

    def handler(request):
        body = json.loads(request.content)
        question = next(m["content"] for m in body["messages"] if m["role"] == "user")
        error_case = "出库 5 件" in question
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={
                "content": "库存不足" if error_case else "已成功完成",
            })])
        return sse_response(tool_call_chunks("write_1", "adjust_stock", json.dumps({
            "sku": question.split()[1], "delta": -5 if error_case else 5,
        })))

    results, path = await run_baseline(
        [APPROVAL_CASES[0], WRITE_ERROR_CASES[0]], client=make_client(handler), budget=Budget(1),
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    assert all(r.passed for r in results), [r.failed_checks for r in results]
    assert results[0].tool_results[0].succeeded
    assert results[1].tool_results[0].content["error"]["code"] == "insufficient_stock"
    first = json.loads((path.with_suffix("") / "app-01.json").read_text(encoding="utf-8"))
    second = json.loads((path.with_suffix("") / "adv-24.json").read_text(encoding="utf-8"))
    assert first["state_after"]["stocks"] != second["state_before"]["stocks"]


def test_cli_rejects_case_from_other_suite_before_reading_model_config(monkeypatch):
    import sys

    from evals.__main__ import main

    monkeypatch.setattr(sys, "argv", ["evals", "--suite", "write-errors", "--case", "app-01"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


async def test_write_error_observation_rejects_compensated_unauthorized_writes(tmp_path):
    from evals.baseline import run_baseline
    from evals.write_cases import WRITE_ERROR_CASES

    def handler(request):
        body = json.loads(request.content)
        question = next(m["content"] for m in body["messages"] if m["role"] == "user")
        round_number = sum(m["role"] == "tool" for m in body["messages"])
        if round_number == 3:
            return sse_response([chunk(delta={"content": "库存不足"})])
        return sse_response(tool_call_chunks(f"write_{round_number}", "adjust_stock", json.dumps({
            "sku": question.split()[1], "delta": [-5, 5, -5][round_number],
        })))

    results, path = await run_baseline(
        WRITE_ERROR_CASES[:1], client=make_client(handler), budget=Budget(1),
        report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces",
    )
    evidence = json.loads((path.with_suffix("") / "adv-24.json").read_text(encoding="utf-8"))
    assert evidence["state_before"] == evidence["state_after"]
    assert not results[0].passed
    assert any("successful_tool_counts: adjust_stock" in f for f in results[0].failed_checks)


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
