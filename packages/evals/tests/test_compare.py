"""跨模型对照聚合单测：汇总指标、按模型去重、markdown 渲染（纯数据，不烧 token）。"""

import json
from copy import deepcopy

import pytest
from evals.compare import (
    ModelRun,
    compare_markdown,
    compatibility_key,
    load_report,
    pick_latest_per_model,
    summarize,
)


def _report(model: str, ts: str, results: list[dict], revision: str = "abc1234567890") -> dict:
    return {
        "model": model,
        "ts": ts,
        "provenance": {"revision": revision},
        "suite": {},
        "results": results,
    }


def _case(passed: bool, *, cost=0.001, tokens=100, ms=500.0, attempts=1, failed=None) -> dict:
    return {
        "case_id": "x",
        "category": "single",
        "passed": passed,
        "failed_checks": failed or [],
        "total_tokens": tokens,
        "cost": cost,
        "duration_ms": ms,
        "attempts": attempts,
    }


def test_summarize_aggregates_executed_only() -> None:
    report = _report("glm-5.3-flash", "20261009-100000", [
        _case(True, cost=0.001, tokens=100, ms=400.0, attempts=1),
        _case(False, cost=0.002, tokens=200, ms=600.0, attempts=2),
        _case(False, failed=["budget: 预算熔断，本条未执行"]),  # 跳过
        _case(False, failed=["not_run: 尚未执行"]),  # 跳过
    ])
    run = summarize(report, report_name="r.json")
    assert run.executed == 2
    assert run.passed == 1
    assert run.success_rate == 0.5
    assert run.total_cost == 0.003
    assert run.cost_complete is True
    assert run.total_tokens == 300
    assert run.duration_ms == 1000.0
    assert run.retries == 1  # attempts 2 -> 1 retry
    assert run.revision == "abc123456789"  # 截断到 12 位


def test_summarize_marks_incomplete_cost_when_none() -> None:
    report = _report("deepseek-chat", "20261009-110000", [
        _case(True, cost=None, tokens=100),
        _case(True, cost=0.002, tokens=100),
    ])
    run = summarize(report)
    assert run.total_cost == 0.002  # 只累计已知成本
    assert run.cost_complete is False


def test_summarize_empty_executed_has_no_rate() -> None:
    report = _report("qwen-plus", "20261009-120000", [
        _case(False, failed=["budget: x"]),
    ])
    run = summarize(report)
    assert run.executed == 0
    assert run.success_rate is None
    assert run.cost_complete is False


def test_pick_latest_per_model_keeps_newest() -> None:
    runs = [
        summarize(_report("m1", "20261009-100000", [_case(True)]), report_name="old.json"),
        summarize(_report("m1", "20261009-130000", [_case(False)]), report_name="new.json"),
        summarize(_report("m2", "20261009-110000", [_case(True)]), report_name="m2.json"),
    ]
    latest = pick_latest_per_model(runs)
    assert [r.model for r in latest] == ["m1", "m2"]  # 按模型名排序
    m1 = next(r for r in latest if r.model == "m1")
    assert m1.report == "new.json"


def test_compare_markdown_renders_rows_and_footnote() -> None:
    runs = [
        summarize(_report("glm-5.3-flash", "t", [_case(True, cost=0.001)]), report_name="g.json"),
        summarize(_report("deepseek-chat", "t", [_case(True, cost=None)]), report_name="d.json"),
    ]
    md = compare_markdown(runs)
    assert "| glm-5.3-flash |" in md
    assert "| deepseek-chat |" in md
    assert "≈¥0.0010 " in md  # glm 成本完整，无 *
    assert "≈¥0.0000*" in md  # deepseek 缺成本，标 *
    assert "估价不完整" in md  # 脚注出现（M06-05 起覆盖缺计量与估价两种原因）


def test_compare_markdown_no_footnote_when_complete() -> None:
    runs = [summarize(_report("glm-5.3-flash", "t", [_case(True, cost=0.001)]))]
    md = compare_markdown(runs)
    assert "*" not in md


def test_load_report_reads_file(tmp_path) -> None:
    path = tmp_path / "20261009-140000-abcdef.json"
    path.write_text(
        json.dumps(_report("glm-5.3-flash", "20261009-140000", [_case(True)])),
        encoding="utf-8",
    )
    run = load_report(path)
    assert isinstance(run, ModelRun)
    assert run.model == "glm-5.3-flash"
    assert run.report == path.name


# ---- M06-05：严格兼容对照（compatibility_key + --strict + 币种列） ----

_B5_ATTRS = (("suite", "b5_scorer_version"), ("suite", "policy_corpus_sha256"),
             ("suite", "policy_index_sha256"))


def _full_report(model="deepseek-v4.1-flash", *, billing=True, scenario="baseline") -> dict:
    suite = {
        "kind": "targeted", "planned": 2, "executed": 2,
        "case_ids": ["b5-eligible", "b5-ineligible"]
        if scenario == "b5" else ["c1", "c2"],
        "scorer_version": "v2.6",
        "retry_policy": "wrapper-no-rerun",
        "case_sha256": "f" * 64,
        "seed": {"value": 20260930, "now": "2026-10-07T12:00:00+08:00",
                 "fresh_database_per_case": True},
        "complete": True,
        "billing_budget": None if not billing else {
            "currency": "USD", "limit": "0.10", "spent": "0.02",
            "requests": 4, "unknown_usage": False,
        },
        "state_checks": 0, "case_traces": {},
    }
    if scenario == "b5":
        suite["b5_scorer_version"] = "b5-1"
        suite["policy_corpus_sha256"] = "c" * 64
        suite["policy_index_sha256"] = "d" * 64
    results = []
    for cid in suite["case_ids"]:
        results.append({
            "case_id": cid, "category": "multi", "passed": True, "failed_checks": [],
            "total_tokens": 100, "cost": None if billing else 0.002,
            "cost_complete": not billing,
            "duration_ms": 500.0, "attempts": 1,
            "billing": {
                "currency": "USD", "estimated_amount": "0.0100",
                "pricing_source": "t", "cache_assumption": "uncached_upper_bound",
                "complete": True,
            } if billing else None,
        })
    return {
        "model": model, "ts": "20261014-110000",
        "provenance": {"revision": "abc1234567890", "source_sha256": "e" * 64,
                       "working_tree_dirty": False},
        "suite": suite, "results": results,
    }


def test_compatibility_key_equal_for_compatible_pair() -> None:
    a = _full_report("glm-5.3-flash")
    b = _full_report("deepseek-v4.1-flash")
    assert compatibility_key(a) == compatibility_key(b)


@pytest.mark.parametrize("mutation", [
    "source_sha256", "case_sha256", "scorer_version", "seed", "retry_policy",
    "retry_policy_missing", "billing_budget_missing", "billing_currency",
])
def test_compatibility_key_changes_with_each_dimension(mutation: str) -> None:
    a = _full_report("glm-5.3-flash")
    b = deepcopy(a)
    if mutation == "source_sha256":
        b["provenance"]["source_sha256"] = "9" * 64
    elif mutation == "case_sha256":
        b["suite"]["case_sha256"] = "9" * 64
    elif mutation == "scorer_version":
        b["suite"]["scorer_version"] = "v2.5"
    elif mutation == "seed":
        b["suite"]["seed"] = {"value": 1, "now": "x", "fresh_database_per_case": True}
    elif mutation == "retry_policy":
        b["suite"]["retry_policy"] = "runner-retry"
    elif mutation == "retry_policy_missing":
        del b["suite"]["retry_policy"]
        with pytest.raises(ValueError):
            compatibility_key(b)
        return
    elif mutation == "billing_budget_missing":
        del b["suite"]["billing_budget"]
    elif mutation == "billing_currency":
        # 币种差异允许成对严格对照（M06-06 的 GLM/CNY vs DeepSeek/USD）：
        # 兼容键相等；对照表按各自币种列示，不做跨币种总额。
        assert compatibility_key(a) == compatibility_key(b), mutation
        return


def test_compatibility_key_b5_scope_requires_policy_fingerprints() -> None:
    a = _full_report("glm-5.3-flash", scenario="b5")
    b = _full_report("deepseek-v4.1-flash", scenario="b5")
    assert compatibility_key(a) == compatibility_key(b)
    missing = deepcopy(a)
    del missing["suite"]["policy_index_sha256"]
    with pytest.raises(ValueError, match="policy_index_sha256"):
        compatibility_key(missing)
    # 用例集里有 b5- 前缀却没有 b5 判据版本：同样拒绝
    hybrid = _full_report("glm-5.3-flash")
    hybrid["suite"]["case_ids"] = ["b5-eligible"]
    with pytest.raises(ValueError, match="b5_scorer_version"):
        compatibility_key(hybrid)


def test_compatibility_key_rejects_reports_without_provenance() -> None:
    legacy = _report("glm-5.3-flash", "t", [_case(True)])
    with pytest.raises(ValueError, match="provenance"):
        compatibility_key(legacy)


def test_strict_cli_requires_pair_and_completeness(tmp_path, capsys) -> None:
    from evals.compare import main as compare_main

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_full_report("glm-5.3-flash")), encoding="utf-8")
    b.write_text(json.dumps(_full_report("deepseek-v4.1-flash")), encoding="utf-8")
    compare_main([str(a), str(b), "--strict"])
    out = capsys.readouterr().out
    assert "$0.0200 USD" in out

    changed = _full_report("deepseek-v4.1-flash")
    changed["suite"]["case_sha256"] = "9" * 64
    b.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SystemExit) as ctx:
        compare_main([str(a), str(b), "--strict"])
    assert ctx.value.code == 1

    partial = _full_report("deepseek-v4.1-flash")
    partial["suite"]["complete"] = False
    partial["suite"]["executed"] = 1
    b.write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(SystemExit) as ctx:
        compare_main([str(a), str(b), "--strict"])
    assert ctx.value.code == 1


def test_strict_cli_rejects_single_report_and_reports_value_errors(tmp_path) -> None:
    from evals.compare import main as compare_main

    only = tmp_path / "only.json"
    only.write_text(json.dumps(_full_report("glm-5.3-flash")), encoding="utf-8")
    with pytest.raises(SystemExit):
        compare_main([str(only), "--strict"])


def test_compare_rows_show_currency_and_unmeasured() -> None:
    runs = [
        summarize(_full_report("glm-5.3-flash"), report_name="g.json"),
    ]
    md = compare_markdown(runs, strict=True)
    assert "≈$0.0200 USD" in md
    assert "≈¥" not in md.replace("≈¥0.0000", "")

    unmeasured_report = _full_report("x")
    unmeasured_report["results"][0]["billing"] = {
        "currency": "USD", "estimated_amount": None,
        "pricing_source": "t", "cache_assumption": "unknown_usage", "complete": False,
    }
    md2 = compare_markdown([summarize(unmeasured_report, report_name="x.json")], strict=True)
    assert "未计量" in md2
    assert "$0.0100 USD" in md2  # 已知部分单独显示，不虚报 0
