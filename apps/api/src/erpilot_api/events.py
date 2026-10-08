"""SSE 事件协议 v1：AgentEvent → (event 名, JSON payload dict)。

前端按 event 名分发（实现见 apps/web/src/protocol.ts，两侧字段必须同步改）：

- start             {session_id, model}                  流开始（含服务端分配的会话 id）
- step              {step}                               第 n 轮 LLM 生成开始
- delta             {text}                               增量回复文本
- reasoning_delta   {step, text}                         增量思考文本（端点支持且开启时才有）
- tool_started      {id, name, arguments}                已取得完整工具调用请求（尚未执行）
- tool_executing    {id, name}                           参数校验通过且审批放行，handler 即将执行
- tool_finished     {id, name, content, ok, display}     工具执行完毕（display 为展示适配器产物，
                                                         未知工具/契约不符时为 null）
- approval_pending  {call_id, pending_id, tool, risk, arguments}   写调用等待人工审批；
                                                             流在此挂起，POST /api/chat/approve
                                                             回填决策后继续
- approval_resolved {call_id, pending_id, tool, approved, reason}  决策已回填
- done              {steps, completed, usage, cost, duration_ms, trace}   一次 run 收口
- error             {message}                            服务端异常（随后流关闭）
- heartbeat         {at}                                 连接存活信号（阶段二）；不代表业务进度，
                                                             不落展示日志

step_end 不下发——它是 trace 的计量记录，前端无需感知。
思考内容只保存在当前页面内存，不进 messages/checkpoint/展示日志（设计 5.1）。
"""

import json
from typing import Any

from agent_core.events import (
    AgentEvent,
    ApprovalPending,
    ApprovalResolved,
    LoopEnd,
    ReasoningDelta,
    ReconciliationPending,
    StepEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    ToolExecuting,
)
from agent_core.llm import TextDelta


def encode_event(event: AgentEvent) -> tuple[str, dict[str, Any]] | None:
    """编码为 SSE 事件；返回 None 表示该事件不下发。"""
    match event:
        case StepStarted(step=step):
            return "step", {"step": step}
        case ReasoningDelta(step=step, text=text):
            return "reasoning_delta", {"step": step, "text": text}
        case TextDelta(text=text):
            return "delta", {"text": text}
        case ToolCallStarted(call=call):
            return "tool_started", {"id": call.id, "name": call.name, "arguments": call.arguments}
        case ToolExecuting(call_id=cid, name=name):
            return "tool_executing", {"id": cid, "name": name}
        case ToolCallFinished(call_id=cid, name=name, content=content, ok=ok, display=display):
            return "tool_finished", {
                "id": cid,
                "name": name,
                "content": content,
                "ok": ok,
                "display": display,
            }
        case ApprovalPending(
            call_id=cid, pending_id=pid, tool=name, risk=risk,
            arguments=args, expires_at=expires_at,
        ):
            return "approval_pending", {
                "call_id": cid,
                "pending_id": pid,
                "tool": name,
                "risk": risk,
                "arguments": args,
                **({"expires_at": expires_at} if expires_at is not None else {}),
            }
        case ReconciliationPending(
            call_id=cid, client_token=token, code=code, message=message
        ):
            return "reconciliation_pending", {
                "call_id": cid,
                "client_token": token,
                "code": code,
                "message": message,
            }
        case ApprovalResolved(
            call_id=cid, pending_id=pid, tool=name, approved=approved, reason=reason
        ):
            return "approval_resolved", {
                "call_id": cid,
                "pending_id": pid,
                "tool": name,
                "approved": approved,
                "reason": reason,
            }
        case StepEnd() | LoopEnd():
            return None
    return None  # pragma: no cover —— match 已穷尽 AgentEvent


def sse_frame(event: str, payload: dict[str, Any]) -> dict[str, str]:
    """sse-starlette 的帧格式：{"event": name, "data": json 字符串}。"""
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}
