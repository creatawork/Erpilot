"""runner：跑一条 case —— 真 LLM + MCP 工具 + trace 落盘 + 计量。

设计（ADR-0004）：pytest 就是 runner——每条 case 一个用例，marker `eval`
默认排除（普通 `uv run pytest` 永不烧 token），显式 `-m eval` 才跑真实链路；
预算熔断由 Budget 承担：累计成本达上限后余下 case 跳过，报告照常落盘。

瞬态重试：上游 5xx / 限流 / 连接超时类错误（如中转端点的
"upstream service timeout"）自动重跑该 case——这些是基础设施抖动，不是
agent 能力问题，不该计入成功率分母；确定性错误（4xx 等）不重试直接判失败。
"""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from agent_core.demo_tools import SYSTEM_PROMPT
from agent_core.llm import LLMClient, TextDelta, Usage
from agent_core.loop import AgentLoop, LoopEnd, StepEnd, ToolCallFinished
from agent_core.trace import JsonlTraceRecorder, new_trace_path

from evals.checks import evaluate_case
from evals.model import CaseResult, EvalCase


class Budget:
    """成本熔断（计划 §7：CI 回归只跑便宜模型 + 设预算上限）。"""

    def __init__(self, limit_cny: float) -> None:
        self.limit_cny = limit_cny
        self.spent_cny = 0.0

    @property
    def exhausted(self) -> bool:
        return self.spent_cny >= self.limit_cny

    def record(self, cost: float | None) -> None:
        if cost is not None:
            self.spent_cny += cost


_TRANSIENT_MARKERS = (
    "timeout", "timed out", "server_error", "rate limit", "overloaded", "connection",
)


def is_transient(exc: Exception) -> bool:
    """上游瞬态错误判定：重试有意义的才返回 True。

    覆盖两类来源：openai SDK 的分类异常（5xx / 限流 / 连接与超时），以及
    中转端点经流式通道抛回的裸 APIError——其 message 里带
    "[server_error] upstream service timeout" 之类的瞬态文案。
    """
    from openai import APIConnectionError, APIStatusError, RateLimitError

    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    text = str(exc).casefold()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _run_error_result(
    case: EvalCase, exc: Exception, tool_calls: list[str], duration_ms: float, attempts: int
) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        category=case.category,
        passed=False,
        failed_checks=[f"run_error: {type(exc).__name__}: {exc}"],
        steps=0,
        completed=False,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
        attempts=attempts,
        error=f"{type(exc).__name__}: {exc}",
    )


async def run_case(
    case: EvalCase,
    *,
    client: LLMClient,
    tools: Sequence,
    resolved: dict[str, str],
    trace_dir: Path,
    sinks: Sequence = (),
    retries: int = 2,
    backoff: float = 2.0,
) -> tuple[CaseResult, Path]:
    """跑一条已解析占位符的 case，返回 (结果, trace 路径)。

    任何异常都不外抛——评测 runner 挂了比评测失败更糟；异常进 CaseResult.error
    记为失败，trace 的 run_error 同步留痕。瞬态错误自动重跑（每次尝试重建
    messages 与 recorder，同一路径追加写，重试过程在 trace 里可回溯）；
    被放弃的尝试已烧掉的 token 无 LoopEnd 计量，成本按成功 attempt 计。
    """
    case = case.format_with(resolved)
    trace_path = new_trace_path(trace_dir, client.config.model)
    started = time.perf_counter()

    for attempt in range(1, retries + 2):
        agent = AgentLoop(client, tools=list(tools))
        messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": case.question},
        ]
        recorder = JsonlTraceRecorder(trace_path, client.config.model, sinks=list(sinks))

        tool_calls: list[str] = []
        step_text: list[str] = []
        final_text = ""
        steps = 0
        completed = False
        usage: Usage | None = None
        try:
            async for event in recorder.run(agent, messages):
                match event:
                    case TextDelta(text=t):
                        step_text.append(t)
                    case StepEnd(step=_, duration_ms=_):
                        text = "".join(step_text)
                        if text.strip():
                            final_text = text  # 最终回答 = 最后一个有内容步骤的文本
                        step_text.clear()
                    case ToolCallFinished(name=name, content=_, ok=_):
                        tool_calls.append(name)
                    case LoopEnd(steps=s, usage=u, completed=c):
                        steps, usage, completed = s, u, c
        except Exception as exc:  # 评测失败 ≠ runner 崩溃：留痕后按失败计
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if attempt <= retries and is_transient(exc):
                await asyncio.sleep(backoff * attempt)
                continue
            return _run_error_result(case, exc, tool_calls, duration_ms, attempt), trace_path

        failed = evaluate_case(
            case, tool_calls=tool_calls, final_text=final_text, steps=steps, completed=completed
        )
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            passed=not failed,
            failed_checks=failed,
            steps=steps,
            completed=completed,
            tool_calls=tool_calls,
            total_tokens=usage.total_tokens if usage else 0,
            cost=_cost_of(client, usage),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            attempts=attempt,
        )
        return result, trace_path

    raise AssertionError("unreachable：重试循环必须 return 或 continue")


def _cost_of(client: LLMClient, usage: Usage | None) -> float | None:
    from agent_core.prices import cost_of

    return cost_of(client.config.model, usage)


async def run_all(
    cases: Sequence[EvalCase],
    *,
    client: LLMClient,
    tools: Sequence,
    resolved: dict[str, str],
    trace_dir: Path,
    sinks: Sequence = (),
    budget: Budget | None = None,
) -> AsyncIterator[tuple[CaseResult, Path]]:
    """顺序跑一批 case；预算熔断后余下 case 直接记失败（原因写明）不调 API。"""
    for case in cases:
        if budget is not None and budget.exhausted:
            yield (
                CaseResult(
                    case_id=case.id,
                    category=case.category,
                    passed=False,
                    failed_checks=[
                        f"budget: 预算熔断（已花 ¥{budget.spent_cny:.4f} ≥ 上限 "
                        f"¥{budget.limit_cny:.2f}），本条未执行"
                    ],
                ),
                Path(),
            )
            continue
        result, trace_path = await run_case(
            case, client=client, tools=tools, resolved=resolved,
            trace_dir=trace_dir, sinks=sinks,
        )
        if budget is not None:
            budget.record(result.cost)
        yield result, trace_path
