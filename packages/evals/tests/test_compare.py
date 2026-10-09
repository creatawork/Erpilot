"""跨模型对照聚合单测：汇总指标、按模型去重、markdown 渲染（纯数据，不烧 token）。"""

import json

from evals.compare import (
    ModelRun,
    compare_markdown,
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
    assert "缺价目表估算" in md  # 脚注出现


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
