"""真实链路评测：真 LLM + MCP 真数据工具，逐 case 判分 + 预算熔断 + 报告落盘。

默认不跑（根 pyproject 的 addopts 排除 marker `eval`；无 API key 也自动跳过）。
显式运行：

    uv run pytest packages/evals -m eval          # 全量 case（成本 ≈ 每条 ¥0.001）
    ERPILOT_EVAL_BUDGET=0.02 uv run pytest packages/evals -m eval
    uv run pytest packages/evals -m eval -k adv-05   # 定点复测单条

报告：reports/evals/<时间戳>.md + .json（标注标准 §6；回归对比以报告为准）。
Langfuse keys 齐全时评测 trace 同步双写远程（ADR-0003 收尾路径）。
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig
from agent_core.observability import langfuse_sink_from_env
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from evals.cases import ALL_CASES
from evals.report import write_report
from mcp_erp import build_agent_tools

# evals 包内测试也要拿到仓库根的 .env（CLI 有自己的加载入口，pytest 没有）
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
def live_db(tmp_path_factory) -> Path:
    """全量种子库（300 商品 / 600 订单，与 data/erpilot.db 同口径），模块级一份。"""
    db = tmp_path_factory.mktemp("evals-live") / "erp.db"
    seed_database(db)
    return db


@pytest.fixture(scope="module")
def live_tools(live_db) -> list:
    return build_agent_tools(live_db)


@pytest.fixture(scope="module")
def live_resolved(live_db) -> dict[str, str]:
    from evals.context import resolve

    return resolve(ErpRepository(make_engine(live_db)))


@pytest.fixture(scope="module")
def live_client() -> LLMClient:
    return LLMClient(LLMConfig.from_env())


@pytest.fixture(scope="module")
def live_sink():
    """Langfuse keys 齐全 → 评测 trace 同步双写远程；未配置/未装 SDK 返回 None。"""
    try:
        return langfuse_sink_from_env()
    except Exception as exc:  # 评测不因远程可观测失败
        print(f"[evals] Langfuse 双写未启用：{exc}")
        return None


@pytest.fixture(scope="module")
def acc() -> SimpleNamespace:
    """模块级记账：Budget + 逐 case 结果 + 起始时间（报告 fixture 消费）。"""
    from evals import Budget

    return SimpleNamespace(budget=Budget(BUDGET_CNY), results=[], t0=time.monotonic())


@pytest.fixture(scope="module", autouse=True)
def _write_report(request, acc, live_client):
    """模块结束时（含中途熔断/失败）落报告。"""
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
    print(f"\n[evals] 报告已写入 {path}")


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.id for c in ALL_CASES])
async def test_case(case, live_client, live_tools, live_resolved, live_sink, acc) -> None:
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
    )
    acc.budget.record(result.cost)
    acc.results.append(result)
    assert result.passed, (
        f"{result.case_id} 未通过：{result.failed_checks}\n"
        f"trace: {trace_path}（erpilot replay 可回放）"
    )
