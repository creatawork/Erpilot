import argparse
import asyncio
import os
from pathlib import Path

from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig
from openai import AsyncOpenAI

from evals.baseline import SUITES, run_baseline, select_cases
from evals.runner import Budget


def main() -> None:
    if (dotenv := find_dotenv(Path.cwd())) is not None:
        load_dotenv(dotenv)
    parser = argparse.ArgumentParser(description="固定范围的 Erpilot 完整/定点评测")
    parser.add_argument("--budget", type=float,
                        default=float(os.environ.get("ERPILOT_EVAL_BUDGET", "0.1")))
    parser.add_argument("--case", action="append", default=[], help="case id，可重复；缺省完整集")
    parser.add_argument("--suite", choices=list(SUITES), default="baseline",
                        help=("baseline 原基线；write-errors 写错误观察集；"
                              "write-preflight 写前安全预检"))
    parser.add_argument("--timeout", type=float, default=30, help="单次模型请求超时秒数")
    args = parser.parse_args()
    try:
        cases = select_cases(args.suite, args.case)
    except ValueError as exc:
        parser.error(str(exc))
    if args.budget <= 0 or args.timeout <= 0:
        parser.error("预算/超时须为正数")
    config = LLMConfig.from_env()
    # 单层重试：runner 可见每次尝试，避免 SDK 隐式重试扩大耗时与费用。
    sdk = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url,
                      timeout=args.timeout, max_retries=0)
    results, path = asyncio.run(run_baseline(
        cases, client=LLMClient(config, client=sdk), budget=Budget(args.budget),
        report_dir=Path("reports/evals"), trace_dir=Path("traces/evals"),
    ))
    print(f"Report: {path}")
    raise SystemExit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
