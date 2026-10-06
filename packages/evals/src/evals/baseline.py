"""统一回归入口：固定 case 范围、单次预算、隔离业务库，输出一份基线。"""

import hashlib
import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from agent_core.approval import AutoDenyGate
from agent_core.demo_tools import system_prompt
from agent_core.llm import LLMClient
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from mcp_erp import build_agent_tools_async

from evals.approval_cases import APPROVAL_CASES
from evals.approval_policy import ScriptedPolicyGate
from evals.cases import ALL_CASES
from evals.context import resolve
from evals.model import CaseResult, EvalCase
from evals.report import source_provenance, write_report
from evals.runner import Budget, run_case
from evals.state import snapshot
from evals.write_cases import WRITE_CASES, WRITE_ERROR_CASES, WRITE_PREFLIGHT_CASES

BASELINE_CASES = [*ALL_CASES, *WRITE_CASES, *APPROVAL_CASES]
SUITES = {
    "baseline": BASELINE_CASES,
    "write-errors": WRITE_ERROR_CASES,
    "write-preflight": WRITE_PREFLIGHT_CASES,
}


def select_cases(suite: str, case_ids: Sequence[str] = ()) -> list[EvalCase]:
    """选集保持固定顺序；错误观察集必须显式选择，不扩大原 35 条分母。"""
    if suite not in SUITES:
        raise ValueError(f"未知 suite: {suite}")
    unknown = set(case_ids) - {case.id for case in SUITES[suite]}
    if unknown:
        raise ValueError(f"{suite} 中未知 case: {sorted(unknown)}")
    return [case for case in SUITES[suite] if not case_ids or case.id in case_ids]


async def run_baseline(
    cases: Sequence[EvalCase], *, client: LLMClient, budget: Budget,
    report_dir: Path, trace_dir: Path,
) -> tuple[list[CaseResult], Path]:
    results = []
    started = time.monotonic()
    approval_ids = {c.id for c in APPROVAL_CASES}
    error_ids = {c.id for c in WRITE_ERROR_CASES}
    preflight_ids = {c.id for c in WRITE_PREFLIGHT_CASES}
    policy_ids = approval_ids | error_ids
    write_ids = {c.id for c in WRITE_CASES} | policy_ids | preflight_ids
    suite = (
        "write-errors" if cases and all(c.id in error_ids for c in cases)
        else "write-preflight" if cases and all(c.id in preflight_ids for c in cases)
        else "baseline"
    )
    provenance = source_provenance()
    metadata = {
        "name": suite,
        "kind": "full" if [c.id for c in cases] == [c.id for c in SUITES[suite]] else "targeted",
        "planned": len(cases), "executed": 0,
        "case_ids": [c.id for c in cases],
        "case_sha256": hashlib.sha256(
            "\n".join(c.model_dump_json() for c in cases).encode()
        ).hexdigest(),
        "seed": "seed_database defaults; fresh database per case",
        "state_checks": sum(c.state is not None for c in cases),
        "complete": False,
        "evidence": {},
    }

    def checkpoint(path=None):
        pending = [CaseResult(
            case_id=c.id, category=c.category, passed=False,
            failed_checks=["not_run: 尚未执行（运行中或已中断）"],
        ) for c in cases[len(results):]]
        metadata["executed"] = sum(
            not any(f.startswith("budget:") for f in r.failed_checks) for r in results
        )
        metadata["complete"] = len(results) == len(cases)
        return write_report(
            report_dir, [*results, *pending], model=client.config.model,
            budget_limit_cny=budget.limit_cny, elapsed_s=time.monotonic() - started,
            metadata=metadata, provenance=provenance, report_path=path,
        )

    path = checkpoint()
    with tempfile.TemporaryDirectory(prefix="erpilot-baseline-") as directory:
        for case in cases:
            if budget.exhausted:
                results.append(CaseResult(
                    case_id=case.id, category=case.category, passed=False,
                    failed_checks=["budget: 预算熔断，本条未执行"],
                ))
                checkpoint(path)
                continue
            db = Path(directory) / f"{case.id}.db"
            seed_database(db)
            engine = make_engine(db)
            try:
                writes = case.id in write_ids
                gate = ScriptedPolicyGate() if case.id in policy_ids else AutoDenyGate()
                tools = await build_agent_tools_async(db, writes=writes, approval_gate=gate)
                resolved = resolve(ErpRepository(engine))
                before = snapshot(engine) if writes else None
                result, trace_path = await run_case(
                    case, client=client, tools=tools, resolved=resolved,
                    trace_dir=trace_dir, system_prompt=system_prompt(writes),
                    state_engine=engine if writes else None,
                )
                evidence_dir = path.with_suffix("")
                evidence_dir.mkdir(exist_ok=True)
                evidence_path = evidence_dir / f"{case.id}.json"
                evidence_path.write_text(json.dumps({
                    "case_id": case.id, "suite": suite,
                    "question": case.format_with(resolved).question,
                    "database": db.name, "trace_path": str(trace_path.resolve()),
                    "approval_gate": type(gate).__name__,
                    "approval_evidence": (
                        "scripted_policy" if case.id in policy_ids
                        else "auto_deny" if writes else "none"
                    ),
                    "human_approval": False,
                    "approval_decisions": [
                        {"request": asdict(request), "decision": asdict(decision)}
                        for request, decision in gate.decisions
                    ] if isinstance(gate, ScriptedPolicyGate) else [],
                    "state_before": before,
                    "state_after": snapshot(engine) if writes else None,
                    "result": result.model_dump(mode="json"),
                }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                metadata["evidence"][case.id] = evidence_path.relative_to(path.parent).as_posix()
                results.append(result)
                budget.record(result.cost)
                print(f"{case.id}: {'PASS' if result.passed else 'FAIL'} "
                      f"({result.steps} steps, {result.attempts} attempts)", flush=True)
                if not result.passed:
                    print(result.failed_checks, flush=True)
                checkpoint(path)
            finally:
                engine.dispose()
    metadata["source_changed_during_run"] = (
        source_provenance()["source_sha256"] != provenance["source_sha256"]
    )
    checkpoint(path)
    return results, path
