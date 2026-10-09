"""跨模型评测对照：聚合 reports/evals/*.json，按模型并排成功率/成本/延迟/重试。

批次 1（M04）的对照表产物：确定性、可复跑、不烧 token。每个模型取其各自的原始
报告，不混比；同一模型有多份报告时默认取最新（ts 最大），也可显式传文件。

用法：
    uv run python -m evals.compare                      # 聚合 reports/evals 下各模型最新报告
    uv run python -m evals.compare reports/evals/a.json reports/evals/b.json
    uv run python -m evals.compare --all reports/evals  # 不去重，列出全部报告
"""

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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

    @property
    def success_rate(self) -> float | None:
        return self.passed / self.executed if self.executed else None


def summarize(report: dict, *, report_name: str = "") -> ModelRun:
    """把一份报告 JSON 汇总为 ModelRun；跳过预算熔断/未执行的占位结果。"""
    results = report.get("results", [])
    executed = [r for r in results if not _skipped(r)]
    costs = [r.get("cost") for r in executed]
    known = [c for c in costs if c is not None]
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


def compare_markdown(runs: Sequence[ModelRun]) -> str:
    """并排对照表；成本列缺价目估算时标 * 并在脚注说明。"""
    lines = [
        "# 跨模型评测对照",
        "",
        "| 模型 | 成功率 | 执行 | 成本 | tokens | 延迟(s) | 重试 | revision | 报告 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    any_incomplete = False
    for r in runs:
        rate = f"{r.success_rate * 100:.0f}%" if r.success_rate is not None else "-"
        cost = f"≈¥{r.total_cost:.4f}"
        if not r.cost_complete:
            cost += "*"
            any_incomplete = True
        lines.append(
            f"| {r.model} | {rate} | {r.passed}/{r.executed} | {cost} | {r.total_tokens} "
            f"| {r.duration_ms / 1000:.0f} | {r.retries} | {r.revision} | {r.report} |"
        )
    if any_incomplete:
        lines += ["", "> \\* 该模型有 case 缺价目表估算（prices.py 未收录），成本不完整。"]
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
    parser.add_argument("--output", type=Path, help="写入 markdown 文件（缺省打印到 stdout）")
    args = parser.parse_args(argv)
    paths = _collect_paths(args.paths or ["reports/evals"])
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
