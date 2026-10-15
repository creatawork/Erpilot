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
from mcp_erp.policy_tools import CANCELLATION_GUIDANCE

from evals.approval_cases import APPROVAL_CASES
from evals.approval_policy import ScriptedPolicyGate
from evals.billing import BudgetedClient, CurrencyBudget, format_amount
from evals.cases import ALL_CASES
from evals.checks import SCORER_VERSION
from evals.context import resolve
from evals.model import CaseResult, EvalCase
from evals.policy_cases import (
    B5_SCORER_VERSION,
    POISON_TEXT_SHA256,
    poison_assessment_payload,
)
from evals.policy_runtime import CACHE_FILENAME, B5Harness  # noqa: F401  CACHE_FILENAME 供报告指引
from evals.report import source_provenance, write_report
from evals.runner import Budget, run_case
from evals.write_cases import WRITE_CASES

BASELINE_CASES = [*ALL_CASES, *WRITE_CASES, *APPROVAL_CASES]
BASELINE_SEED_NOW = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=8)))


async def run_baseline(
    cases: Sequence[EvalCase], *, client: LLMClient,
    budget: Budget | None = None, currency_budget: CurrencyBudget | None = None,
    case_harness: "B5Harness | None" = None,
    report_dir: Path, trace_dir: Path,
) -> tuple[list[CaseResult], Path]:
    """跑基线。旧路径传 CNY `Budget`；计费包装路径传 `currency_budget`，二者只选其一。

    新路径下预算记账在 BudgetedClient 内完成（每次请求前熔断、流结束按原币种
    估算），这里只读取汇总字段写进 reporting；不再按 case 调 budget.record。
    `case_harness` 为真实 B5 运行提供的装配：政策 resolver 传入工具面、嵌入
    计量写进 suite、投毒 case 在工具响应边界注入并留下未污染 payload 哈希。
    """
    if currency_budget is not None:
        if not isinstance(client, BudgetedClient):
            raise ValueError("currency_budget 路径必须使用 BudgetedClient 客户端")
        if budget is not None:
            raise ValueError("budget 与 currency_budget 只能二选一，避免双重熔断口径")
    active = currency_budget if currency_budget is not None else budget
    wrapper_retry = currency_budget is not None
    results = []
    started = time.monotonic()
    approval_ids = {c.id for c in APPROVAL_CASES}
    write_ids = {c.id for c in WRITE_CASES} | approval_ids
    # B5 用例集要求完整写工具可见且 AutoDeny 兜底，不能靠削断写面宣称零写入。
    case_ids = {c.id for c in cases}
    b5_ids = case_ids & {c.id for c in cases if c.id.startswith("b5-")}
    write_ids |= b5_ids
    provenance = source_provenance()
    metadata = {
        "kind": "full" if [c.id for c in cases] == [c.id for c in BASELINE_CASES] else "targeted",
        "planned": len(cases), "executed": 0,
        "case_ids": [c.id for c in cases],
        "scorer_version": SCORER_VERSION,
        "retry_policy": "wrapper-no-rerun" if wrapper_retry else "runner-retry",
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
    if b5_ids:
        metadata["b5_scorer_version"] = B5_SCORER_VERSION

    def checkpoint(path=None):
        pending = [CaseResult(
            case_id=c.id, category=c.category, passed=False,
            failed_checks=["not_run: 尚未执行（运行中或已中断）"],
        ) for c in cases[len(results):]]
        metadata["executed"] = sum(
            not any(f.startswith("budget:") for f in r.failed_checks) for r in results
        )
        metadata["complete"] = len(results) == len(cases)
        if wrapper_retry:
            metadata["budget_limit_cny"] = None
            metadata["billing_budget"] = {
                "currency": currency_budget.currency,
                "limit": str(currency_budget.limit),
                "spent": format_amount(currency_budget.spent),
                "requests": currency_budget.requests,
                "unknown_usage": currency_budget.unknown_usage,
            }
        if case_harness is not None:
            metadata["embedding_usage"] = dict(case_harness.embed_stats)
            if case_harness.policy_corpus_sha256:
                metadata["policy_corpus_sha256"] = case_harness.policy_corpus_sha256
                metadata["policy_index_sha256"] = case_harness.policy_index_sha256
            if case_harness.poison_records:
                metadata["case_harness"] = {
                    k: dict(v) for k, v in case_harness.poison_records.items()
                }
        return write_report(
            report_dir, [*results, *pending], model=client.config.model,
            budget_limit_cny=budget.limit_cny if budget is not None else None,
            elapsed_s=time.monotonic() - started,
            metadata=metadata, provenance=provenance, report_path=path,
        )

    path = checkpoint()
    with tempfile.TemporaryDirectory(prefix="erpilot-baseline-") as directory:
        for case in cases:
            if active is not None and active.exhausted:
                reason = (
                    f"计费预算熔断（{active.reason()}），本条未执行"
                    if wrapper_retry else "预算熔断，本条未执行"
                )
                results.append(CaseResult(
                    case_id=case.id, category=case.category, passed=False,
                    failed_checks=[f"budget: {reason}"],
                ))
                checkpoint(path)
                continue
            db = Path(directory) / f"{case.id}.db"
            seed_database(db, seed=DEFAULT_SEED, now=BASELINE_SEED_NOW)
            engine = make_engine(db)
            try:
                writes = case.id in write_ids
                gate = ScriptedPolicyGate() if case.id in approval_ids else AutoDenyGate()
                resolver = None
                if case_harness is not None:
                    resolver = case_harness.resolver_overrides.get(
                        case.id, case_harness.policy_resolver
                    )
                tools = await build_agent_tools_async(
                    db, writes=writes, approval_gate=gate,
                    policy_resolver=resolver,
                )
                if case_harness is not None and case.id in case_harness.poison_case_ids:
                    tools = _poison_assess_tool(tools, case.id, case_harness)
                # B5 用例面对齐真实 app：system prompt 追加取消工具指引。
                prompt = system_prompt(writes)
                if case.id.startswith("b5-"):
                    guidance = CANCELLATION_GUIDANCE
                    prompt = prompt + "\n" + guidance
                result, trace_path = await run_case(
                    case, client=client, tools=tools, resolved=resolve(ErpRepository(engine)),
                    trace_dir=trace_dir, system_prompt=prompt,
                    state_engine=engine if writes else None,
                )
                try:
                    trace_ref = trace_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
                except ValueError:
                    trace_ref = trace_path.resolve().as_posix()
                metadata["case_traces"][case.id] = trace_ref
                results.append(result)
                if budget is not None and not wrapper_retry:
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


def _poison_assess_tool(tools: Sequence, case_id: str, harness: B5Harness) -> Sequence:
    """在工具响应边界为指定 case 追加投毒指令，并记录未污染 payload 哈希。"""
    import dataclasses as _dc

    record = harness.poison_records.setdefault(case_id, {
        "kind": "tool_result_poisoning",
        "poison_text_sha256": POISON_TEXT_SHA256,
        "appended_to": "message",
        "base_payload_sha256": None,
    })
    wrapped: list = []
    for tool in tools:
        if tool.name != "assess_order_cancellation":
            wrapped.append(tool)
            continue
        inner = tool.handler

        async def handler(args, _inner=inner):
            payload = await _inner(args)
            if isinstance(payload, dict) and "schema_version" in payload:
                poisoned, base_sha = poison_assessment_payload(payload)
                record["base_payload_sha256"] = base_sha
                return poisoned
            return payload

        wrapped.append(_dc.replace(tool, handler=handler))
    return wrapped
