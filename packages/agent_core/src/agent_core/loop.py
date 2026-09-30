"""M1 第 2–4 周产出：手写 agent loop——工具调用循环 + 防护 + 并行 + 错误策略。

第 2 周范围（计划 §6）：
1. 工具循环：tools schema 注入请求 → 解析 tool_calls → Pydantic 校验入参 → asyncio 执行
   → 结果回填 → 继续生成，直到模型给出不含工具调用的最终回答
2. 防护：max_steps 防死循环；单工具执行超时 asyncio.wait_for
3. 事件流：TextDelta / ToolCallStarted / ToolCallFinished / LoopEnd

第 3 周增强（计划 §6）：
- 并行工具调用：同一轮多个 tool_calls 用 asyncio.as_completed 并发执行，完成一个转发一个
- 错误回填策略 v1：结构化错误格式 {"error": {"type", "message"}}；validation/unknown_tool
  是确定性错误直接回填让模型修正或改道，timeout/execution 视为瞬态按重试策略重试
- 上下文压缩：每步请求前按 ContextPolicy 截断/压缩历史（见 context.py）

第 4 周增强（计划 §6）：
- 轮次边界事件：StepStarted / StepEnd（每步的开始与 LLM 生成的 usage/耗时），
  供 trace 逐轮记录与 SSE/前端的"第 n 轮"展示；ToolCallFinished 补 call_id，
  消费方可把 started/finished 精确配对
- 事件消费：trace.py（JSONL 落盘）、cli.py（rich 渲染）、apps/api（SSE 转发）
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionMessageParam

from agent_core.context import ContextPolicy, compress_messages
from agent_core.llm import LLMClient, StreamEnd, TextDelta, ToolCall, Usage
from agent_core.tools import Tool


@dataclass(frozen=True, slots=True)
class ToolRetryPolicy:
    """工具瞬态错误（timeout/execution）的自动重试策略。

    validation / unknown_tool 是确定性错误，重试无意义——直接回填让模型
    修正参数或改道，不受本策略影响。
    """

    retries: int = 1
    backoff: float = 0.5  # 第 n 次重试前等待 backoff * n 秒
    retry_on: frozenset[str] = frozenset({"timeout", "execution"})


def _error_payload(kind: str, message: str) -> str:
    """回填给模型的错误信息格式 v1：结构化 JSON，模型可稳定解析。"""
    return json.dumps(
        {"error": {"type": kind, "message": message}}, ensure_ascii=False
    )


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """模型请求了一次工具调用，即将执行。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """工具执行完毕；ok=False 时 content 是回填给模型的错误信息。

    call_id 与 ToolCallStarted.call.id 对应——并行调用完成顺序不定，
    消费方靠它把 started/finished 精确配对。
    """

    call_id: str
    name: str
    content: str
    ok: bool = True


@dataclass(frozen=True, slots=True)
class StepStarted:
    """一轮 LLM 请求开始（step 从 1 计）。"""

    step: int


@dataclass(frozen=True, slots=True)
class StepEnd:
    """一轮 LLM 生成结束：本步 usage 与耗时（含该步前的上下文压缩）。"""

    step: int
    usage: Usage | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class LoopEnd:
    """循环终止事件；completed=False 表示触发 max_steps 防护。"""

    steps: int
    usage: Usage | None = None
    completed: bool = True


AgentEvent = (
    TextDelta | StepStarted | StepEnd | ToolCallStarted | ToolCallFinished | LoopEnd
)


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_steps: int = 8
    tool_timeout: float = 30.0  # 秒；单工具执行上限（asyncio.wait_for）
    retry: ToolRetryPolicy = field(default_factory=ToolRetryPolicy)
    context: ContextPolicy = field(default_factory=ContextPolicy)


def _merge_usage(a: Usage | None, b: Usage | None) -> Usage | None:
    """跨步合计 usage；两边都缺时保持 None。"""
    if a is None:
        return b
    if b is None:
        return a
    return Usage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        total_tokens=a.total_tokens + b.total_tokens,
    )


def _assistant_toolcall_message(text: str, tool_calls: list[ToolCall]) -> dict[str, object]:
    """把本步的 assistant 输出（文本 + 工具调用）写回消息历史的协议格式。"""
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tool_calls
        ],
    }


class AgentLoop:
    """无框架的 agent 主循环：流式生成 → 工具调用 → 回填 → 再生成。"""

    def __init__(
        self,
        client: LLMClient,
        tools: Sequence[Tool] = (),
        config: LoopConfig | None = None,
    ) -> None:
        names = [t.name for t in tools]
        if len(names) != len(set(names)):
            raise ValueError(f"工具重名：{names}")
        self._client = client
        self._tools = {t.name: t for t in tools}
        self._config = config or LoopConfig()

    async def run(
        self, messages: list[ChatCompletionMessageParam]
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮 agent 循环，逐个产出 AgentEvent。

        中间产生的 assistant / tool 消息会**就地追加**进传入的 messages；
        历史超过 ContextPolicy 预算时也会**就地裁剪**——需要完整历史做展示/
        追溯的场景，调用方自行留存副本。
        """
        schemas = [t.openai_schema() for t in self._tools.values()] or None
        total_usage: Usage | None = None
        for step in range(1, self._config.max_steps + 1):
            t0 = time.perf_counter()
            yield StepStarted(step=step)
            compress_messages(messages, self._config.context)
            parts: list[str] = []
            calls: list[ToolCall] = []
            step_usage: Usage | None = None
            async for event in self._client.stream_chat(messages, tools=schemas):
                match event:
                    case TextDelta(text=text):
                        parts.append(text)
                        yield event
                    case ToolCall() as call:
                        calls.append(call)
                    case StreamEnd(usage=usage):
                        step_usage = usage
            total_usage = _merge_usage(total_usage, step_usage)
            yield StepEnd(
                step=step,
                usage=step_usage,
                duration_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

            if not calls:  # 本步不含工具调用，即最终回答
                messages.append({"role": "assistant", "content": "".join(parts)})
                yield LoopEnd(steps=step, usage=total_usage, completed=True)
                return

            messages.append(_assistant_toolcall_message("".join(parts), calls))
            # 本轮所有工具调用并行执行：Started 按调用顺序产出，Finished 按**完成
            # 顺序**产出（asyncio.as_completed），结果消息按调用顺序回填历史
            for call in calls:
                yield ToolCallStarted(call=call)
            outcomes: list[tuple[str, bool]] = [("", True)] * len(calls)
            pending = [self._execute_indexed(i, c) for i, c in enumerate(calls)]
            for done in asyncio.as_completed(pending):
                index, content, ok = await done
                outcomes[index] = (content, ok)
                yield ToolCallFinished(
                    call_id=calls[index].id, name=calls[index].name, content=content, ok=ok
                )
            for call, (content, _ok) in zip(calls, outcomes, strict=True):
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
        yield LoopEnd(steps=self._config.max_steps, usage=total_usage, completed=False)

    async def _execute_indexed(
        self, index: int, call: ToolCall
    ) -> tuple[int, str, bool]:
        content, ok = await self._execute(call)
        return index, content, ok

    async def _execute(self, call: ToolCall) -> tuple[str, bool]:
        """执行一次工具调用，永不抛出：异常按策略重试，最终转成结构化错误回填。

        错误格式 v1：{"error": {"type": "validation|unknown_tool|timeout|execution",
        "message": ...}}。validation / unknown_tool 是确定性错误，立即回填让模型
        修正参数或改道；timeout / execution 视为瞬态，按 ToolRetryPolicy 重试。
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return _error_payload("unknown_tool", f"未注册的工具：{call.name}"), False
        try:
            args = tool.params_model.model_validate_json(call.arguments)
        except Exception as exc:
            return _error_payload("validation", f"参数校验失败，请修正参数后重试：{exc}"), False

        policy = self._config.retry
        last_error = ""
        for attempt in range(policy.retries + 1):
            if attempt:
                await asyncio.sleep(policy.backoff * attempt)
            kind = ""
            try:
                result = await asyncio.wait_for(tool.handler(args), self._config.tool_timeout)
            except TimeoutError:
                kind = "timeout"
                retried = f"已重试 {attempt} 次" if attempt else "未重试"
                last_error = _error_payload(
                    "timeout", f"工具执行超时（>{self._config.tool_timeout}s，{retried}）"
                )
            except Exception as exc:
                kind = "execution"
                last_error = _error_payload("execution", f"工具执行出错：{exc}")
            else:
                if isinstance(result, str):
                    return result, True
                return json.dumps(result, ensure_ascii=False, default=str), True
            if kind not in policy.retry_on:  # 该类错误重试无意义，直接回填让模型改道
                break
        return last_error, False
