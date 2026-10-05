"""写操作真实链路评测：写工具面 + AutoDenyGate + 写提示词，逐 case 判分。

与读评测集（test_evals_live.py）分开跑——工具面、系统提示词、考的行为都
不同（M4 ADR-0005 决策 6）：评测环境无真人，审批门拒绝一切写调用，考的
是"未批准不得假装执行、如实转述"。

显式运行：

    uv run pytest packages/evals -m eval -k write          # 3 条写 case
    ERPILOT_EVAL_BUDGET=0.02 uv run pytest packages/evals -m eval -k write

报告与读评测集落同一目录（reports/evals/<时间戳>.md），文件名可区分。
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core.approval import AutoDenyGate
from agent_core.demo_tools import WRITES_PROMPT
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig
from agent_core.observability import langfuse_sink_from_env
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from evals.report import write_report
from evals.write_cases import WRITE_CASES
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


@pytest.fixture()
def live_db(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("evals-write-live") / "erp.db"
    seed_database(db)
    return db


@pytest.fixture()
def live_tools(live_db) -> list:
    return build_agent_tools(live_db, writes=True, approval_gate=AutoDenyGate())


@pytest.fixture()
def live_resolved(live_db) -> dict[str, str]:
    from evals.context import resolve

    return resolve(ErpRepository(make_engine(live_db)))


@pytest.fixture(scope="module")
def live_client() -> LLMClient:
    return LLMClient(LLMConfig.from_env())


@pytest.fixture(scope="module")
def live_sink():
    try:
        return langfuse_sink_from_env()
    except Exception as exc:
        print(f"[evals-write] Langfuse 双写未启用：{exc}")
        return None


@pytest.fixture(scope="module")
def acc() -> SimpleNamespace:
    from evals import Budget

    return SimpleNamespace(budget=Budget(BUDGET_CNY), results=[], t0=time.monotonic())


@pytest.fixture(scope="module", autouse=True)
def _write_report(request, acc, live_client):
    yield
    if not acc.results:
        return
    path = write_report(
        _REPO_ROOT / "reports" / "evals",
        acc.results,
        model=live_client.config.model,
        budget_limit_cny=BUDGET_CNY,
        elapsed_s=time.monotonic() - acc.t0,
    )
    print(f"\n[evals-write] 报告已写入 {path}")


@pytest.mark.parametrize("case", WRITE_CASES, ids=[c.id for c in WRITE_CASES])
async def test_write_case(
    case, live_client, live_tools, live_resolved, live_sink, live_db, acc
) -> None:
    from evals.runner import run_case

    if acc.budget.exhausted:
        pytest.skip(
            f"预算熔断：已花 ¥{acc.budget.spent_cny:.4f} ≥ 上限 ¥{BUDGET_CNY:.2f}"
        )

    result, trace_path = await run_case(
        case,
        client=live_client,
        tools=live_tools,
        resolved=live_resolved,
        trace_dir=_REPO_ROOT / "traces" / "evals",
        sinks=[live_sink] if live_sink else (),
        system_prompt=WRITES_PROMPT,
        state_engine=make_engine(live_db),
    )
    acc.budget.record(result.cost)
    acc.results.append(result)
    assert result.passed, (
        f"{result.case_id} 未通过：{result.failed_checks}\n"
        f"trace: {trace_path}（erpilot replay 可回放）"
    )
