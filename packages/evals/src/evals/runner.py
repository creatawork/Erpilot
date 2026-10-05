"""runner：跑一条 case —— 真 LLM + MCP 工具 + trace 落盘 + 计量。

设计（ADR-0004）：pytest 就是 runner——每条 case 一个用例，marker `eval`
默认排除（普通 `uv run pytest` 永不烧 token），显式 `-m eval` 才跑真实链路；
预算熔断由 Budget 承担：累计成本达上限后余下 case 跳过，报告照常落盘。

瞬态重试：上游 5xx / 限流 / 连接超时类错误（如中转端点的
"upstream service timeout"）自动重跑该 case——这些是基础设施抖动，不是
agent 能力问题，不该计入成功率分母；确定性错误（4xx 等）不重试直接判失败。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from agent_core.demo_tools import SYSTEM_PROMPT
from agent_core.llm import LLMClient, TextDelta, Usage
from agent_core.loop import AgentLoop, LoopEnd, StepEnd, ToolCallFinished, ToolCallStarted
from agent_core.trace import JsonlTraceRecorder, new_trace_path
from sqlalchemy.engine import Engine

from evals.checks import evaluate_case
from evals.model import CaseResult, EvalCase, ToolResult
from evals.state import check_state, snapshot


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
    case: EvalCase, exc: Exception, tool_calls: list[str], duration_ms: float, attempts: int,
    *, usage: Usage | None = None, cost: float | None = None,
    tool_results: Sequence[ToolResult] = (),
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
        total_tokens=usage.total_tokens if usage else 0,
        cost=cost, cost_complete=False, tool_results=list(tool_results),
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
    system_prompt: str = SYSTEM_PROMPT,
    state_engine: Engine | None = None,
) -> tuple[CaseResult, Path]:
    """跑一条已解析占位符的 case，返回 (结果, trace 路径)。

    任何异常都不外抛——评测 runner 挂了比评测失败更糟；异常进 CaseResult.error
    记为失败，trace 的 run_error 同步留痕。瞬态错误自动重跑（每次尝试重建
    messages 与 recorder，同一路径追加写，重试过程在 trace 里可回溯）；
    计量累计所有尝试已返回的 StepEnd usage；未返回 usage 的费用注明缺失。
    system_prompt 默认读工具面口径；写工具面评测传 system_prompt(True)
    （M4 ADR-0005）。
    """
    case = case.format_with(resolved)
    trace_path = new_trace_path(trace_dir, client.config.model)
    started = time.perf_counter()
    before = snapshot(state_engine) if case.state and state_engine is not None else None
    # 整轮重跑可能重复模型已发起的写操作；写工具面仅重试工具执行，不重跑任务。
    if any(t.risk for t in tools):
        retries = 0
    measured = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    cost_complete = True
    all_tool_results: list[ToolResult] = []

    for attempt in range(1, retries + 2):
        agent = AgentLoop(client, tools=list(tools))
        messages: list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": case.question},
        ]
        recorder = JsonlTraceRecorder(trace_path, client.config.model, sinks=list(sinks))

        tool_calls: list[str] = []
        tool_results: list[ToolResult] = []
        arguments: dict[str, dict] = {}
        step_text: list[str] = []
        visible_parts: list[str] = []
        steps = 0
        completed = False
        try:
            async for event in recorder.run(agent, messages):
                match event:
                    case TextDelta(text=t):
                        step_text.append(t)
                    case StepEnd(step=_, duration_ms=_, usage=step_usage):
                        if step_usage is None:
                            cost_complete = False
                        else:
                            measured = Usage(
                                prompt_tokens=measured.prompt_tokens + step_usage.prompt_tokens,
                                completion_tokens=(
                                    measured.completion_tokens + step_usage.completion_tokens
                                ),
                                total_tokens=measured.total_tokens + step_usage.total_tokens,
                            )
                        text = "".join(step_text)
                        visible_parts.append(text)  # v2 判分范围：全部助手可见步骤文本
                        step_text.clear()
                    case ToolCallStarted(call=call):
                        try:
                            arguments[call.id] = json.loads(call.arguments)
                        except (ValueError, TypeError):
                            arguments[call.id] = {}
                    case ToolCallFinished(call_id=cid, name=name, content=content, ok=ok):
                        tool_calls.append(name)
                        try:
                            payload = json.loads(content)
                        except (ValueError, TypeError):
                            payload = content
                        tool_results.append(ToolResult(
                            call_id=cid, name=name, arguments=arguments.get(cid, {}),
                            content=payload, ok=ok,
                        ))
                        all_tool_results.append(tool_results[-1])
                    case LoopEnd(steps=s, completed=c):
                        steps, completed = s, c
        except Exception as exc:  # 评测失败 ≠ runner 崩溃：留痕后按失败计
            cost_complete = False  # 中断的生成可能已经计费，但未返回 usage。
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if attempt <= retries and is_transient(exc):
                await asyncio.sleep(backoff * attempt)
                continue
            result = _run_error_result(
                case, exc, tool_calls, duration_ms, attempt, usage=measured,
                cost=_cost_of(client, measured), tool_results=all_tool_results,
            )
            if case.state and before is not None and state_engine is not None:
                result.failed_checks.extend(check_state(case.state, before, snapshot(state_engine)))
            return result, trace_path

        failed = evaluate_case(
            case, tool_calls=tool_calls, visible_text="".join(visible_parts),
            steps=steps, completed=completed,
        )
        for name in dict.fromkeys(t.name for t in tool_results if not t.ok):
            if not any(t.name == name and t.succeeded for t in tool_results):
                failed.append(f"tool_error: {name} 执行异常且无成功恢复证据")
        for name in case.expect_successful_tools:
            if not any(t.name == name and t.succeeded for t in tool_results):
                failed.append(f"expect_successful_tools: {name} 无成功执行证据")
        observed_codes = {
            t.content["error"].get("code")
            for t in tool_results
            if isinstance(t.content, dict) and isinstance(t.content.get("error"), dict)
        }
        for code in case.expect_error_codes:
            if code not in observed_codes:
                failed.append(f"expect_error_codes: 未观测到 {code}")
        if case.state:
            if before is None or state_engine is None:
                failed.append("state: 缺少数据库状态证据")
            else:
                failed.extend(check_state(case.state, before, snapshot(state_engine)))
        for name, expected_count in case.successful_tool_counts.items():
            count = sum(t.name == name and t.succeeded for t in tool_results)
            if count != expected_count:
                failed.append(
                    f"successful_tool_counts: {name} 期望 {expected_count} 次，实际 {count}"
                )
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            passed=not failed,
            failed_checks=failed,
            steps=steps,
            completed=completed,
            tool_calls=tool_calls,
            total_tokens=measured.total_tokens,
            cost=_cost_of(client, measured),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            attempts=attempt,
            tool_results=all_tool_results,
            cost_complete=cost_complete and _cost_of(client, measured) is not None,
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
