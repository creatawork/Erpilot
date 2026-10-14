import argparse
import asyncio
import os
from decimal import Decimal
from pathlib import Path

from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig
from openai import AsyncOpenAI

from evals.baseline import BASELINE_CASES, run_baseline
from evals.billing import BudgetedClient, CurrencyBudget, endpoint_rate
from evals.runner import Budget


def main() -> None:
    if (dotenv := find_dotenv(Path.cwd())) is not None:
        load_dotenv(dotenv)
    parser = argparse.ArgumentParser(description="固定范围的 Erpilot 完整/定点评测")
    parser.add_argument("--budget", type=float,
                        default=float(os.environ.get("ERPILOT_EVAL_BUDGET", "0.1")))
    parser.add_argument("--budget-currency", default=None, choices=["CNY", "USD"],
                        help="按原币种计费预算运行（缺省保留旧 CNY cost 预算路径）")
    parser.add_argument("--case", action="append", default=[], help="case id，可重复；缺省完整集")
    parser.add_argument("--timeout", type=float, default=30, help="单次模型请求超时秒数")
    args = parser.parse_args()
    cases = [c for c in BASELINE_CASES if not args.case or c.id in args.case]
    unknown = set(args.case) - {c.id for c in cases}
    if unknown or args.budget <= 0 or args.timeout <= 0:
        parser.error(f"未知 case {sorted(unknown)} 或预算/超时非正数")
    config = LLMConfig.from_env()
    # 单层重试：runner 可见每次尝试，避免 SDK 隐式重试扩大耗时与费用。
    sdk_timeout = {"timeout": args.timeout, "max_retries": 0}
    if args.budget_currency:
        # 指定币种后先解析价目并校验币种，未知价目早退出；不发任何模型请求。
        rate = endpoint_rate(config.model, config.base_url)
        if rate.currency != args.budget_currency:
            parser.error(
                f"当前模型 {config.model} 的价目币种是 {rate.currency}"
                f"，--budget-currency 应为 {rate.currency}（不同币种不换算也不混算）"
            )
        sdk = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, **sdk_timeout)
        budget = CurrencyBudget(
            Decimal(str(args.budget)), rate.currency,
            max_requests=max(40, 8 * len(cases)),
        )
        client = BudgetedClient(LLMClient(config, client=sdk), rate, budget)
        results, path = asyncio.run(run_baseline(
            cases, client=client, currency_budget=budget,
            report_dir=Path("reports/evals"), trace_dir=Path("traces/evals"),
        ))
    else:
        sdk = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, **sdk_timeout)
        results, path = asyncio.run(run_baseline(
            cases, client=LLMClient(config, client=sdk), budget=Budget(args.budget),
            report_dir=Path("reports/evals"), trace_dir=Path("traces/evals"),
        ))
    print(f"Report: {path}")
    raise SystemExit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
