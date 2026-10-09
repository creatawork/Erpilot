"""Deterministic, read-only aggregation of immutable evaluation reports."""

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[4]
_METRICS = (
    ("success_rate", "Success rate", "%"),
    ("estimated_cost", "Estimated cost", "CNY"),
    ("mean_case_duration_ms", "Mean case latency", "ms"),
)
_COLORS = ("#1677ff", "#d46b08", "#389e0d", "#722ed1", "#c41d7f", "#08979c")


def load_report(path: Path) -> dict[str, Any]:
    """Load and validate one per-run report without mutating it."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid report {path.name}: {exc}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError(f"invalid report {path.name}: expected an object with results[]")
    if "suite" in report and not isinstance(report["suite"], dict):
        raise ValueError(f"invalid report {path.name}: suite must be an object")
    if not isinstance(report.get("model"), str) or not isinstance(report.get("ts"), str):
        raise ValueError(f"invalid report {path.name}: model and ts must be strings")
    for index, row in enumerate(report["results"]):
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError(f"invalid report {path.name}: results[{index}] has no case_id")
        if not isinstance(row.get("passed"), bool):
            raise ValueError(f"invalid report {path.name}: results[{index}].passed must be boolean")
        checks = row.get("failed_checks", [])
        if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
            raise ValueError(f"invalid report {path.name}: results[{index}].failed_checks invalid")
        for key in ("cost", "duration_ms"):
            value = row.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise ValueError(f"invalid report {path.name}: results[{index}].{key} invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"invalid report {path.name}: results[{index}].{key} not finite")
            if value is not None and value < 0:
                raise ValueError(
                    f"invalid report {path.name}: results[{index}].{key} cannot be negative"
                )
    suite = report.get("suite", {})
    for key in ("planned", "executed"):
        if key in suite and (
            not isinstance(suite[key], int) or isinstance(suite[key], bool) or suite[key] < 0
        ):
            raise ValueError(
                f"invalid report {path.name}: suite.{key} must be a non-negative integer"
            )
    if "complete" in suite and not isinstance(suite["complete"], bool):
        raise ValueError(f"invalid report {path.name}: suite.complete must be boolean")
    for key in ("kind", "scorer_version", "case_sha256", "prompt_variant"):
        if key in suite and not isinstance(suite[key], str):
            raise ValueError(f"invalid report {path.name}: suite.{key} must be a string")
    if "case_ids" in suite and (
        not isinstance(suite["case_ids"], list)
        or any(not isinstance(case_id, str) for case_id in suite["case_ids"])
        or len(suite["case_ids"]) != len(set(suite["case_ids"]))
    ):
        raise ValueError(f"invalid report {path.name}: suite.case_ids must be unique strings")
    report["_source"] = path.name
    return report


def cohort_key(report: dict[str, Any]) -> tuple[str, str, str, str, str]:
    suite = report.get("suite") if isinstance(report.get("suite"), dict) else {}
    model = _text(report.get("model"), "unknown")
    scorer = _text(suite.get("scorer_version"), "unknown")
    kind = suite.get("kind") if suite.get("kind") in {"full", "targeted"} else "unknown"
    if suite.get("prompt_variant") in {"control", "treatment", "standard"}:
        variant = suite["prompt_variant"]
    else:
        variant = "standard" if kind != "unknown" else "unknown"
    fingerprint = suite.get("case_sha256")
    if not isinstance(fingerprint, str) or not fingerprint:
        ids = suite.get("case_ids")
        if isinstance(ids, list) and ids and all(isinstance(item, str) for item in ids):
            fingerprint = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
        else:
            fingerprint = "unknown"
    return model, scorer, kind, variant, fingerprint


def summarize_report(report: dict[str, Any], *, source: str) -> dict[str, Any]:
    results = report["results"]
    executed = [
        row
        for row in results
        if not any(
            check.startswith(("budget:", "not_run:")) for check in row.get("failed_checks", [])
        )
    ]
    suite = report.get("suite") if isinstance(report.get("suite"), dict) else {}
    passed = sum(row["passed"] for row in executed)
    known_costs = [row["cost"] for row in executed if row.get("cost") is not None]
    cost_complete = (
        bool(executed)
        and len(known_costs) == len(executed)
        and all(row.get("cost_complete", True) for row in executed)
    )
    durations = [float(row.get("duration_ms") or 0) for row in executed]
    incomplete_reasons: list[str] = []
    completion: str
    if suite.get("complete") is True:
        completion = "complete"
    elif suite.get("complete") is False:
        completion = "incomplete"
        incomplete_reasons.append("suite.complete=false")
    elif isinstance(suite.get("planned"), int) and isinstance(suite.get("executed"), int):
        if suite["planned"] != suite["executed"]:
            completion = "incomplete"
            incomplete_reasons.append("planned/executed mismatch")
        elif suite.get("complete") is True:
            completion = "complete"
        else:
            completion = "unknown"
    elif suite.get("complete") is True:
        completion = "complete"
    else:
        completion = "unknown"
    if suite.get("source_changed_during_run") is True:
        completion = "incomplete"
        incomplete_reasons.append("source changed during run")
    key = cohort_key(report)
    return {
        "source": source,
        "timestamp": report.get("ts", "unknown"),
        "model": key[0],
        "scorer_version": key[1],
        "suite_kind": key[2],
        "prompt_variant": key[3],
        "case_fingerprint": key[4],
        "comparable": key[1] != "unknown" and key[4] != "unknown",
        "revision": _text((report.get("provenance") or {}).get("revision"), "unknown")
        if isinstance(report.get("provenance"), dict)
        else "unknown",
        "source_sha256": _text((report.get("provenance") or {}).get("source_sha256"), "unknown")
        if isinstance(report.get("provenance"), dict)
        else "unknown",
        "planned": suite.get("planned", len(results)),
        "reported_executed": suite.get("executed"),
        "executed": len(executed),
        "passed": passed,
        "success_rate": passed / len(executed) if executed else None,
        "estimated_cost": float(sum(known_costs)),
        "cost_complete": cost_complete,
        "duration_ms": float(sum(durations)),
        "mean_case_duration_ms": sum(durations) / len(durations) if durations else None,
        "completion": completion,
        "incomplete_run": completion == "incomplete",
        "incomplete_reasons": incomplete_reasons,
    }


def build_trends(paths: list[Path] | tuple[Path, ...]) -> dict[str, Any]:
    reports = [load_report(path) for path in sorted(paths, key=lambda item: (item.name, str(item)))]
    if not reports:
        raise ValueError("empty input: no evaluation reports found")
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        key = cohort_key(report)
        grouped[key].append(summarize_report(report, source=report["_source"]))
    cohorts = []
    for key in sorted(grouped):
        observations = sorted(grouped[key], key=lambda row: (row["timestamp"], row["source"]))
        cohorts.append(
            {
                "model": key[0],
                "scorer_version": key[1],
                "suite_kind": key[2],
                "prompt_variant": key[3],
                "case_fingerprint": key[4],
                "comparable": all(row["comparable"] for row in observations),
                "observations": observations,
            }
        )
    return {"schema_version": 1, "cohorts": cohorts}


def collect_inputs(paths: list[Path] | tuple[Path, ...] | None = None) -> list[Path]:
    selected = list(paths) if paths else [_ROOT / "reports" / "evals"]
    found: set[Path] = set()
    for path in selected:
        if path.is_file() and path.suffix.lower() == ".json":
            candidates = [path]
        elif path.is_dir():
            candidates = list(path.glob("*.json"))
        else:
            raise ValueError(f"input does not exist or is not JSON: {path}")
        for candidate in candidates:
            if candidate.name.startswith("trends-v"):
                continue
            found.add(candidate)
    return sorted(found, key=lambda item: (item.name, str(item)))


def render_markdown(data: dict[str, Any], *, exclude_incomplete: bool = False) -> str:
    lines = [
        "# Evaluation trends v1",
        "",
        "Cohorts are separated by model, scorer, suite kind, prompt variant and case fingerprint.",
        "Cost values are estimated CNY, not invoices; incomplete values show the known subtotal.",
        "",
        "| Model | Scorer | Suite | Prompt | Cases | Run (executed/planned) | Success | "
        "Est. cost | Cost data | Mean latency (ms) | Completion | Revision | Source |",
        "|---|---|---|---|---|---|---:|---:|---|---:|---|---|---|",
    ]
    rows = [row for cohort in data["cohorts"] for row in cohort["observations"]]
    for row in sorted(rows, key=lambda item: (item["model"], item["timestamp"], item["source"])):
        if exclude_incomplete and row["completion"] != "complete":
            continue
        rate = (
            "—"
            if row["success_rate"] is None
            else f"{row['success_rate'] * 100:.1f}% ({row['passed']}/{row['executed']})"
        )
        latency = (
            "—" if row["mean_case_duration_ms"] is None else f"{row['mean_case_duration_ms']:.1f}"
        )
        cost = f"¥{row['estimated_cost']:.4f}"
        reported_executed = row["reported_executed"]
        executed_count = reported_executed if reported_executed is not None else row["executed"]
        lines.append(
            f"| {row['model']} | {row['scorer_version']} | {row['suite_kind']} | "
            f"{row['prompt_variant']} | `{row['case_fingerprint'][:12]}` | "
            f"{row['timestamp']} ({executed_count}/{row['planned']}) | "
            f"{rate} | {cost} | {'complete' if row['cost_complete'] else '不完整'} | {latency} | "
            f"{row['completion']} | `{row['revision'][:12]}` | `{row['source']}` |"
        )
    if len(lines) == 6:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — | — | no observations |")
    lines += [
        "",
        "## Cohort dimensions",
        "",
        "- Model",
        "- Scorer version",
        "- Suite kind",
        "- Prompt variant",
        "- Ordered case-set fingerprint",
        "",
        "Unknown scorer or case identity is retained as a non-comparable observation.",
        "Budget/not-run cases are excluded from execution metrics; "
        "attempted failures remain failures.",
        "",
    ]
    return "\n".join(lines)


def render_svg(data: dict[str, Any], *, exclude_incomplete: bool = False) -> str:
    width, height = 960, 570
    panel_h = 165
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        "<style>text{font:12px sans-serif;fill:#222}.title{font-size:16px;font-weight:bold}"
        ".grid{stroke:#ddd;stroke-width:1}</style>",
        '<text x="20" y="22" class="title">Evaluation trends v1 · separate cohorts</text>',
    ]
    cohorts = data["cohorts"]
    for panel, (metric, label, unit) in enumerate(_METRICS):
        top = 35 + panel * panel_h
        lines.append(f'<text x="20" y="{top + 15}" class="title">{label} ({unit})</text>')
        left, right, plot_top, plot_bottom = 220, 900, top + 25, top + 125
        lines.append(
            f'<line x1="{left}" y1="{plot_bottom}" x2="{right}" y2="{plot_bottom}" class="grid"/>'
        )
        valid = [
            (cohort, row)
            for cohort in cohorts
            for row in cohort["observations"]
            if row[metric] is not None
            and (not exclude_incomplete or row["completion"] == "complete")
        ]
        values = [row[metric] for _, row in valid]
        maximum = max(values, default=1.0) or 1.0
        for series_index, cohort in enumerate(cohorts):
            observations = [
                row
                for row in cohort["observations"]
                if row[metric] is not None
                and (not exclude_incomplete or row["completion"] == "complete")
            ]
            color = _COLORS[series_index % len(_COLORS)]
            points = []
            for index, row in enumerate(observations):
                x = left + (right - left) * (index / max(1, len(observations) - 1))
                y = plot_bottom - (plot_bottom - plot_top) * row[metric] / maximum
                points.append((x, y, row))
            if cohort["comparable"] and len(points) > 1:
                coords = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
                lines.append(
                    f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>'
                )
            for x, y, row in points:
                incomplete_cost = metric == "estimated_cost" and not row["cost_complete"]
                marker = (
                    "#aaa"
                    if row["completion"] != "complete"
                    or not cohort["comparable"]
                    or incomplete_cost
                    else color
                )
                lines.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{marker}">'
                    f"<title>{_xml(row['source'])}: {_xml(str(row[metric]))}; "
                    f"{_xml(row['completion'])}; "
                    f"comparable={str(cohort['comparable']).lower()}</title></circle>"
                )
            name = (
                f"{cohort['model']} · {cohort['scorer_version']} · "
                f"{cohort['suite_kind']} · {cohort['prompt_variant']}"
            )
            legend_y = top + 43 + 13 * series_index
            lines.append(f'<text x="{left}" y="{legend_y}" fill="{color}">{_xml(name)}</text>')
        if not valid:
            lines.append(f'<text x="{left}" y="{plot_top + 20}">no observations</text>')
    lines.append(
        f'<text x="20" y="{height - 10}">Gray points indicate unknown/incomplete runs, '
        "non-comparable cohorts, or incomplete cost data.</text>"
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_artifacts(
    paths: list[Path] | tuple[Path, ...] | None,
    output_dir: Path,
    *,
    exclude_incomplete: bool = False,
) -> dict[str, Path]:
    inputs = collect_inputs(paths)
    data = build_trends(inputs)  # Validate all inputs before creating outputs.
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": output_dir / "trends-v1.json",
        "markdown": output_dir / "trends-v1.md",
        "svg": output_dir / "trends-v1.svg",
    }
    outputs["json"].write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    outputs["markdown"].write_text(
        render_markdown(data, exclude_incomplete=exclude_incomplete), encoding="utf-8"
    )
    outputs["svg"].write_text(
        render_svg(data, exclude_incomplete=exclude_incomplete), encoding="utf-8"
    )
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="report JSON files or directories")
    parser.add_argument("--output-dir", type=Path, default=_ROOT / "reports" / "evals")
    parser.add_argument("--exclude-incomplete", action="store_true")
    args = parser.parse_args(argv)
    try:
        outputs = write_artifacts(
            args.paths, args.output_dir, exclude_incomplete=args.exclude_incomplete
        )
    except ValueError as exc:
        parser.error(str(exc))
    for path in outputs.values():
        print(path)
    return 0


def _text(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
