"""SSE 事件协议 v1：AgentEvent → (event 名, JSON payload dict)。

前端按 event 名分发（实现见 apps/web/src/protocol.ts，两侧字段必须同步改）：

- start         {session_id, model}                  流开始（含服务端分配的会话 id）
- step          {step}                               第 n 轮 LLM 生成开始
- delta         {text}                               增量回复文本
- tool_started  {id, name, arguments}                即将执行工具调用
- tool_finished {id, name, content, ok}              工具执行完毕（ok=False 时 content 是错误）
- done          {steps, completed, usage, cost, duration_ms, trace}   一次 run 收口
- error         {message}                            服务端异常（随后流关闭）

step_end 不下发——它是 trace 的计量记录，前端无需感知。
"""

import json
from typing import Any

from agent_core.llm import TextDelta
from agent_core.loop import (
    AgentEvent,
    LoopEnd,
    StepEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)


def encode_event(event: AgentEvent) -> tuple[str, dict[str, Any]] | None:
    """编码为 SSE 事件；返回 None 表示该事件不下发。"""
    match event:
        case StepStarted(step=step):
            return "step", {"step": step}
        case TextDelta(text=text):
            return "delta", {"text": text}
        case ToolCallStarted(call=call):
            return "tool_started", {"id": call.id, "name": call.name, "arguments": call.arguments}
        case ToolCallFinished(call_id=cid, name=name, content=content, ok=ok):
            return "tool_finished", {"id": cid, "name": name, "content": content, "ok": ok}
        case StepEnd() | LoopEnd():
            return None
    return None  # pragma: no cover —— match 已穷尽 AgentEvent


def sse_frame(event: str, payload: dict[str, Any]) -> dict[str, str]:
    """sse-starlette 的帧格式：{"event": name, "data": json 字符串}。"""
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}
