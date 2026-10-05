"""脚本化审批策略真实链路评测：写工具面 + ScriptedPolicyGate + 写提示词。

与 test_evals_write_live（AutoDenyGate）分开跑——本模块的策略门会放行
低风险额度内的写调用，考"该批的批、不该批的拒"（M4 第 2 周，标注标准
§7 批准确率起步）。跑在临时库上，写完即弃。

显式运行：

    uv run pytest packages/evals -m eval -k approval         # 3 条策略 case
    ERPILOT_EVAL_BUDGET=0.02 uv run pytest packages/evals -m eval -k approval
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core.demo_tools import WRITES_PROMPT
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig
from agent_core.observability import langfuse_sink_from_env
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from evals.approval_cases import APPROVAL_CASES
from evals.approval_policy import ScriptedPolicyGate
from evals.report import write_report
from mcp_erp import build_agent_tools

if (dotenv := find_dotenv(Path(__file__).resolve())) is not None:
    load_dotenv(dotenv)

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"), reason="无 ZHIPU_API_KEY，跳过真实链路评测"
    ),
]

BUDGET_CNY = float(os.environ.get("ERPILOT_EVAL_BUDGET", "1.0"))
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def policy_db(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("evals-approval-live") / "erp.db"
    seed_database(db)
    return db


@pytest.fixture(scope="module")
def policy_gate() -> ScriptedPolicyGate:
    return ScriptedPolicyGate()


@pytest.fixture(scope="module")
def policy_tools(policy_db, policy_gate) -> list:
    return build_agent_tools(policy_db, writes=True, approval_gate=policy_gate)


@pytest.fixture(scope="module")
def policy_resolved(policy_db) -> dict[str, str]:
    from evals.context import resolve

    return resolve(ErpRepository(make_engine(policy_db)))


@pytest.fixture(scope="module")
def policy_client() -> LLMClient:
    return LLMClient(LLMConfig.from_env())


@pytest.fixture(scope="module")
def policy_sink():
    try:
        return langfuse_sink_from_env()
    except Exception as exc:
        print(f"[evals-approval] Langfuse 双写未启用：{exc}")
        return None


@pytest.fixture(scope="module")
def acc() -> SimpleNamespace:
    from evals import Budget

    return SimpleNamespace(budget=Budget(BUDGET_CNY), results=[], t0=time.monotonic())


@pytest.fixture(scope="module", autouse=True)
def _approval_report(request, acc, policy_client):
    yield
    if not acc.results:
        return
    path = write_report(
        _REPO_ROOT / "reports" / "evals",
        acc.results,
        model=policy_client.config.model,
        budget_limit_cny=BUDGET_CNY,
        elapsed_s=time.monotonic() - acc.t0,
    )
    print(f"\n[evals-approval] 报告已写入 {path}")


@pytest.mark.parametrize("case", APPROVAL_CASES, ids=[c.id for c in APPROVAL_CASES])
async def test_approval_case(
    case, policy_client, policy_tools, policy_resolved, policy_sink, acc
) -> None:
    from evals.runner import run_case

    if acc.budget.exhausted:
        pytest.skip(f"预算熔断：已花 ¥{acc.budget.spent_cny:.4f} ≥ 上限 ¥{BUDGET_CNY:.2f}")

    result, trace_path = await run_case(
        case,
        client=policy_client,
        tools=policy_tools,
        resolved=policy_resolved,
        trace_dir=_REPO_ROOT / "traces" / "evals",
        sinks=[policy_sink] if policy_sink else (),
        system_prompt=WRITES_PROMPT,
    )
    acc.budget.record(result.cost)
    acc.results.append(result)
    assert result.passed, (
        f"{result.case_id} 未通过：{result.failed_checks}\n"
        f"trace: {trace_path}（erpilot replay 可回放）"
    )
