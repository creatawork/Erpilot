"""跨模型评测对照：聚合 reports/evals/*.json，按模型并排成功率/成本/延迟/重试。

批次 1（M04）的对照表产物：确定性、可复跑、不烧 token。每个模型取其各自的原始
报告，不混比；同一模型有多份报告时默认取最新（ts 最大），也可显式传文件。

--strict（M06-05）：严格兼容对照——只消费 case 集 / scorer / 来源指纹 / seed /
重试策略完全一致且执行完整的两份原始报告；任何一项缺失或不同都拒绝并置非零
退出。成本列按各报告实际币种列示，不求跨币种总额、不按数值排名；缺估价的
部分如实写"未计量"，不把 0 当完整费用。

用法：
    uv run python -m evals.compare                      # 聚合 reports/evals 下各模型最新报告
    uv run python -m evals.compare reports/evals/a.json reports/evals/b.json
    uv run python -m evals.compare --all reports/evals  # 不去重，列出全部报告
    uv run python -m evals.compare --strict a.json b.json
"""

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_SKIP_PREFIXES = ("budget:", "not_run:")


def _skipped(result: dict) -> bool:
    return any(c.startswith(_SKIP_PREFIXES) for c in result.get("failed_checks", []))


@dataclass(frozen=True, slots=True)
class ModelRun:
    """单份报告按模型汇总后的指标（溯源字段保留，便于回查原始报告）。"""

    model: str
    ts: str
    report: str
    executed: int
    passed: int
    total_cost: float
    cost_complete: bool
    total_tokens: int
    duration_ms: float
    retries: int
    revision: str
    billing: tuple[tuple[str, str, bool], ...] = ()
    billing_unmeasured: int = 0

    @property
    def success_rate(self) -> float | None:
        return self.passed / self.executed if self.executed else None


def summarize(report: dict, *, report_name: str = "") -> ModelRun:
    """把一份报告 JSON 汇总为 ModelRun；跳过预算熔断/未执行的占位结果。"""
    results = report.get("results", [])
    executed = [r for r in results if not _skipped(r)]
    costs = [r.get("cost") for r in executed]
    known = [c for c in costs if c is not None]
    billing: dict[str, list] = {}
    unmeasured = 0
    for r in executed:
        estimate = r.get("billing")
        if not isinstance(estimate, dict) or not estimate.get("currency"):
            continue
        amount = estimate.get("estimated_amount")
        if amount is None:
            unmeasured += 1
            continue
        total, complete = billing.get(estimate["currency"], (Decimal(0), True))
        billing[estimate["currency"]] = (
            total + Decimal(str(amount)), complete and bool(estimate.get("complete"))
        )
    provenance = report.get("provenance", {})
    return ModelRun(
        model=report.get("model", "unknown"),
        ts=report.get("ts", ""),
        report=report_name,
        executed=len(executed),
        passed=sum(1 for r in executed if r.get("passed")),
        total_cost=sum(known),
        cost_complete=bool(executed) and len(known) == len(costs),
        total_tokens=sum(r.get("total_tokens", 0) for r in executed),
        duration_ms=sum(r.get("duration_ms", 0.0) for r in executed),
        retries=sum(max(0, r.get("attempts", 1) - 1) for r in executed),
        revision=(provenance.get("revision") or "unavailable")[:12],
        billing=tuple(
            (currency, str(total), complete)
            for currency, (total, complete) in sorted(billing.items())
        ),
        billing_unmeasured=unmeasured,
    )


def load_report(path: Path) -> ModelRun:
    report = json.loads(path.read_text(encoding="utf-8"))
    return summarize(report, report_name=path.name)


def pick_latest_per_model(runs: Iterable[ModelRun]) -> list[ModelRun]:
    """同一模型保留 ts 最大的一份；输出按模型名排序，稳定可复跑。"""
    latest: dict[str, ModelRun] = {}
    for run in runs:
        current = latest.get(run.model)
        if current is None or run.ts > current.ts:
            latest[run.model] = run
    return sorted(latest.values(), key=lambda r: r.model)


_REQUIRED_KEYS: tuple[tuple[str, str], ...] = (
    ("provenance", "source_sha256"),
    ("suite", "case_sha256"),
    ("suite", "scorer_version"),
    ("suite", "retry_policy"),
    ("suite", "seed"),
)
_B5_EXTRA_KEYS: tuple[tuple[str, str], ...] = (
    ("suite", "b5_scorer_version"),
    ("suite", "policy_corpus_sha256"),
    ("suite", "policy_index_sha256"),
)


def compatibility_key(report: dict) -> str:
    """strict 对照的兼容键：缺失或空值的必备溯源字段直接拒绝，不做 None==None。"""
    parts: list[str] = []
    for section, name in _REQUIRED_KEYS:
        container = report.get(section)
        if not isinstance(container, dict) or name not in container:
            raise ValueError(f"严格对照需要 {section}.{name}，报告缺失该字段")
        value = container[name]
        if value is None or value == "":
            raise ValueError(f"严格对照要求 {section}.{name} 为非空值")
        parts.append(f"{section}.{name}={json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    suite = report.get("suite", {})
    has_b5_scope = any(
        str(cid).startswith("b5-") for cid in suite.get("case_ids", [])
    ) or "b5_scorer_version" in suite
    if has_b5_scope:
        for section, name in _B5_EXTRA_KEYS:
            if not isinstance(suite, dict) or name not in suite or suite[name] in (None, ""):
                raise ValueError(f"严格对照需要 B5 指纹 {name}，报告缺失该字段")
            parts.append(f"{section}.{name}={suite[name]}")
    budget = report.get("suite", {}).get("billing_budget")
    parts.append(
        f"billing.currency={budget['currency']}"
        if isinstance(budget, dict) and budget.get("currency")
        else "billing.currency=none"
    )
    return " | ".join(parts)


def compare_markdown(runs: Sequence[ModelRun], *, strict: bool = False) -> str:
    """并排对照表；成本列按各报告实际币种列示，缺估价标"未计量"。"""
    lines = [
        "# 跨模型评测对照",
        "",
        "| 模型 | 成功率 | 执行 | 成本 | tokens | 延迟(s) | 重试 | revision | 报告 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    any_incomplete = False
    for r in runs:
        rate = f"{r.success_rate * 100:.0f}%" if r.success_rate is not None else "-"
        if r.billing or r.billing_unmeasured:
            cells = [
                f"≈{'¥' if currency == 'CNY' else '$'}{Decimal(amount):.4f} {currency}"
                for currency, amount, _complete in r.billing
            ]
            if r.billing_unmeasured:
                cells.append(f"{r.billing_unmeasured} 条未计量")
            cost = "；".join(cells)
            if any(not complete for _c, _a, complete in r.billing):
                cost += "*"
                any_incomplete = True
        else:
            cost = f"≈¥{r.total_cost:.4f}"
            if not r.cost_complete:
                cost += "*"
                any_incomplete = True
        lines.append(
            f"| {r.model} | {rate} | {r.passed}/{r.executed} | {cost} | {r.total_tokens} "
            f"| {r.duration_ms / 1000:.0f} | {r.retries} | {r.revision} | {r.report} |"
        )
    if any_incomplete:
        lines += ["", "> \\* 该模型有 case 缺计量或估价不完整（缺 usage /估价缺失），成本不完整。"]
    if not strict:
        lines += [
            "",
            "> 本表未开启 --strict：各报告的 case 集 / scorer / 来源指纹可能不同，"
            "不要把本表当作同一口径的严格并排。币种各自列示，不做跨币种总额。",
        ]
    return "\n".join(lines) + "\n"


def _collect_paths(inputs: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.json")))
        else:
            paths.append(p)
    return paths


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="跨模型评测报告对照")
    parser.add_argument("paths", nargs="*", default=["reports/evals"],
                        help="报告 .json 文件或目录（默认 reports/evals）")
    parser.add_argument("--all", action="store_true",
                        help="不按模型去重，列出全部报告")
    parser.add_argument("--strict", action="store_true",
                        help="严格兼容对照：恰好两份报告，溯源/范围指纹必须完全一致")
    parser.add_argument("--output", type=Path, help="写入 markdown 文件（缺省打印到 stdout）")
    args = parser.parse_args(argv)
    paths = _collect_paths(args.paths or ["reports/evals"])
    if args.strict:
        if len(paths) != 2:
            parser.error("--strict 需要恰好两份报告文件（不能用目录聚合）")
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        try:
            left, right = (compatibility_key(r) for r in reports)
        except ValueError as exc:
            sys.stderr.write(f"严格对照拒绝：{exc}\n")
            raise SystemExit(1) from None
        if left != right:
            sys.stderr.write(
                "严格对照拒绝：兼容键不一致\n左侧: " + left + "\n右侧: " + right + "\n"
            )
            raise SystemExit(1)
        partial = [
            p.name for p, r in zip(paths, reports, strict=True)
            if r.get("suite", {}).get("complete") is not True
        ]
        if partial:
            sys.stderr.write(
                "严格对照拒绝：未完整执行范围的报告不可宣称完整对比："
                + ", ".join(partial) + "\n"
            )
            raise SystemExit(1)
        runs = [load_report(p) for p in paths]
        markdown = compare_markdown(runs, strict=True)
        if args.output:
            args.output.write_text(markdown, encoding="utf-8")
            print(f"Wrote: {args.output}")
        else:
            sys.stdout.buffer.write(markdown.encode("utf-8"))
        return
    runs = [load_report(p) for p in paths]
    if not args.all:
        runs = pick_latest_per_model(runs)
    else:
        runs = sorted(runs, key=lambda r: (r.model, r.ts))
    markdown = compare_markdown(runs)
    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
        print(f"Wrote: {args.output}")
    else:
        # ¥ 等非 ASCII 在 Windows GBK 控制台会炸；统一按 utf-8 写 stdout。
        sys.stdout.buffer.write(markdown.encode("utf-8"))


if __name__ == "__main__":
    main()
