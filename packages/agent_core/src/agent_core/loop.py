"""M1 第 2 周产出：手写 agent loop——工具调用循环 + 防护。

第 2 周范围（计划 §6）：
1. 工具循环：tools schema 注入请求 → 解析 tool_calls → Pydantic 校验入参 → asyncio 执行
   → 结果回填 → 继续生成，直到模型给出不含工具调用的最终回答
2. 防护：max_steps 防死循环；单工具执行超时 asyncio.wait_for
3. 事件流：TextDelta / ToolCallStarted / ToolCallFinished / LoopEnd，供第 4 周 SSE 转发

刻意留到第 3 周：并行工具调用（asyncio.gather）、错误回填策略精修（错误信息格式、
何时重试、何时让模型改道）、上下文超长的截断/压缩。
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from openai.types.chat import ChatCompletionMessageParam

from agent_core.llm import LLMClient, StreamEnd, TextDelta, ToolCall, Usage
from agent_core.tools import Tool


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """模型请求了一次工具调用，即将执行。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """工具执行完毕；ok=False 时 content 是回填给模型的错误信息。"""

    name: str
    content: str
    ok: bool = True


@dataclass(frozen=True, slots=True)
class LoopEnd:
    """循环终止事件；completed=False 表示触发 max_steps 防护。"""

    steps: int
    usage: Usage | None = None
    completed: bool = True


AgentEvent = TextDelta | ToolCallStarted | ToolCallFinished | LoopEnd


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_steps: int = 8
    tool_timeout: float = 30.0  # 秒；单工具执行上限（asyncio.wait_for）


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

        中间产生的 assistant / tool 消息会**就地追加**进传入的 messages，
        调用方（第 4 周的会话管理）持有完整对话历史。
        """
        schemas = [t.openai_schema() for t in self._tools.values()] or None
        total_usage: Usage | None = None
        for step in range(1, self._config.max_steps + 1):
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

            if not calls:  # 本步不含工具调用，即最终回答
                messages.append({"role": "assistant", "content": "".join(parts)})
                yield LoopEnd(steps=step, usage=total_usage, completed=True)
                return

            messages.append(_assistant_toolcall_message("".join(parts), calls))
            for call in calls:
                yield ToolCallStarted(call=call)
                content, ok = await self._execute(call)
                yield ToolCallFinished(name=call.name, content=content, ok=ok)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
        yield LoopEnd(steps=self._config.max_steps, usage=total_usage, completed=False)

    async def _execute(self, call: ToolCall) -> tuple[str, bool]:
        """执行一次工具调用，永不抛出：异常转成回填给模型的错误信息。

        这是错误回填策略 v0（格式化文本 + ok 标记）；重试/改道策略第 3 周精修。
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return f"未知工具：{call.name}", False
        try:
            args = tool.params_model.model_validate_json(call.arguments)
        except Exception as exc:
            return f"参数校验失败：{exc}", False
        try:
            result = await asyncio.wait_for(tool.handler(args), self._config.tool_timeout)
        except TimeoutError:
            return f"工具执行超时（>{self._config.tool_timeout}s）", False
        except Exception as exc:
            return f"工具执行出错：{exc}", False
        if isinstance(result, str):
            return result, True
        return json.dumps(result, ensure_ascii=False, default=str), True
