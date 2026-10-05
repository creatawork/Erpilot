"""回归报告：markdown 落 reports/evals/，逐 case 结果 + 分类成功率 + 成本。

成功率曲线的分母（case 集）必须稳定（标注标准 §6）——报告即回归对比的凭据。
"""

import hashlib
import json
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from evals.model import CaseCategory, CaseResult

_CATEGORY_LABELS: dict[CaseCategory, str] = {
    CaseCategory.SINGLE: "单工具",
    CaseCategory.MULTI: "多步",
    CaseCategory.EDGE: "边界",
    CaseCategory.ADVERSARIAL: "对抗",
}


def render_report(
    results: Sequence[CaseResult],
    *,
    model: str,
    budget_limit_cny: float | None = None,
    elapsed_s: float = 0.0,
) -> str:
    executed = [r for r in results if not _skipped(r)]
    passed = [r for r in executed if r.passed]
    total_cost = sum(r.cost or 0.0 for r in results)
    lines = [
        f"# 评测回归报告 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 模型：{model}",
        f"- 总成功率：**{len(passed)}/{len(executed)} = "
        f"{(len(passed) / len(executed) * 100 if executed else 0):.0f}%**"
        f"（执行 {len(executed)} 条"
        + (
            f"，未执行 {len(results) - len(executed)} 条"
            if len(results) > len(executed)
            else ""
        )
        + "）",
        f"- 总成本：≈¥{total_cost:.4f}"
        + (f"（预算上限 ¥{budget_limit_cny:.2f}）" if budget_limit_cny is not None else ""),
        f"- 总耗时：{elapsed_s:.0f}s",
        f"- 重试次数：{sum(max(0, r.attempts - 1) for r in executed)}；"
        f"计量不完整：{sum(not r.cost_complete for r in executed)} 条",
        "- 成本为仓库价目表估算，包含已返回 usage 的失败尝试；"
        "中断生成及 SDK 内部重试可能缺失计量，预算上限不是账单硬上限。",
        "",
        "## 分类成功率",
        "",
        "| 类别 | 通过/执行 | 成功率 |",
        "|---|---|---|",
    ]
    for category in CaseCategory:
        rows = [r for r in executed if r.category is category]
        if not rows:
            continue
        ok = sum(1 for r in rows if r.passed)
        lines.append(
            f"| {_CATEGORY_LABELS[category]} | {ok}/{len(rows)} | {ok / len(rows) * 100:.0f}% |"
        )
    lines += [
        "",
        "## 逐 case 结果",
        "",
        "| case | 结果 | 工具调用 | 步数 | tokens | 成本 | 失败检查 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        mark = "✅" if r.passed else "⏭️ 未执行" if _skipped(r) else "❌"
        tokens = str(r.total_tokens) if r.total_tokens else "-"
        cost = f"≈¥{r.cost:.4f}" if r.cost is not None else "-"
        detail = "; ".join(r.failed_checks) if r.failed_checks else ""
        if r.error and not r.passed:
            detail = f"{r.error}（{detail}）" if detail else r.error
        lines.append(
            f"| {r.case_id} | {mark} | {', '.join(r.tool_calls) or '-'} | {r.steps} "
            f"| {tokens} | {cost} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def _skipped(r: CaseResult) -> bool:
    return any(c.startswith(("budget:", "not_run:")) for c in r.failed_checks)


def write_report(
    report_dir: Path,
    results: Sequence[CaseResult],
    *,
    model: str,
    budget_limit_cny: float | None = None,
    elapsed_s: float = 0.0,
    metadata: dict | None = None,
    provenance: dict | None = None,
    report_path: Path | None = None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = report_path or report_dir / f"{stamp}-{uuid4().hex[:6]}.md"
    provenance = provenance or source_provenance()
    path.write_text(
        render_report(
            results, model=model, budget_limit_cny=budget_limit_cny, elapsed_s=elapsed_s
        ) + "\n## 版本与范围\n\n```json\n"
        + json.dumps({"provenance": provenance, "suite": metadata or {}},
                     ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    # 机器可读版本供曲线聚合（M9 起画成功率趋势）
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": model,
                "ts": stamp,
                "provenance": provenance,
                "suite": metadata or {},
                "results": [json.loads(r.model_dump_json()) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def source_provenance() -> dict:
    root = Path(__file__).resolve().parents[4]

    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        return result.stdout.decode("utf-8", errors="replace").strip()

    digest = hashlib.sha256()
    for name in sorted(git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()):
        path = root / name
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".css", ".toml", ".yml"}:
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return {
        "revision": git("rev-parse", "HEAD") or "unavailable",
        "working_tree_dirty": bool(git("status", "--porcelain")),
        "source_sha256": digest.hexdigest(),
    }
