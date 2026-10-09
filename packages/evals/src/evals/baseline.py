"""统一回归入口：固定 case 范围、单次预算、隔离业务库，输出一份基线。"""

import hashlib
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_core.approval import AutoDenyGate
from agent_core.demo_tools import system_prompt
from agent_core.llm import LLMClient
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import DEFAULT_SEED, seed_database
from mcp_erp import build_agent_tools_async

from evals.approval_cases import APPROVAL_CASES
from evals.approval_policy import ScriptedPolicyGate
from evals.cases import ALL_CASES
from evals.checks import SCORER_VERSION
from evals.context import resolve
from evals.model import CaseResult, EvalCase
from evals.report import source_provenance, write_report
from evals.runner import Budget, run_case
from evals.write_cases import WRITE_CASES

BASELINE_CASES = [*ALL_CASES, *WRITE_CASES, *APPROVAL_CASES]
BASELINE_SEED_NOW = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=8)))


async def run_baseline(
    cases: Sequence[EvalCase], *, client: LLMClient, budget: Budget,
    report_dir: Path, trace_dir: Path,
) -> tuple[list[CaseResult], Path]:
    results = []
    started = time.monotonic()
    approval_ids = {c.id for c in APPROVAL_CASES}
    write_ids = {c.id for c in WRITE_CASES} | approval_ids
    provenance = source_provenance()
    metadata = {
        "kind": "full" if [c.id for c in cases] == [c.id for c in BASELINE_CASES] else "targeted",
        "planned": len(cases), "executed": 0,
        "case_ids": [c.id for c in cases],
        "scorer_version": SCORER_VERSION,
        "case_sha256": hashlib.sha256(
            "\n".join(c.model_dump_json() for c in cases).encode()
        ).hexdigest(),
        "seed": {
            "value": DEFAULT_SEED,
            "now": BASELINE_SEED_NOW.isoformat(),
            "fresh_database_per_case": True,
        },
        "case_traces": {},
        "state_checks": sum(c.state is not None for c in cases),
        "complete": False,
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
            seed_database(db, seed=DEFAULT_SEED, now=BASELINE_SEED_NOW)
            engine = make_engine(db)
            try:
                writes = case.id in write_ids
                gate = ScriptedPolicyGate() if case.id in approval_ids else AutoDenyGate()
                tools = await build_agent_tools_async(db, writes=writes, approval_gate=gate)
                result, trace_path = await run_case(
                    case, client=client, tools=tools, resolved=resolve(ErpRepository(engine)),
                    trace_dir=trace_dir, system_prompt=system_prompt(writes),
                    state_engine=engine if writes else None,
                )
                try:
                    trace_ref = trace_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
                except ValueError:
                    trace_ref = trace_path.resolve().as_posix()
                metadata["case_traces"][case.id] = trace_ref
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
