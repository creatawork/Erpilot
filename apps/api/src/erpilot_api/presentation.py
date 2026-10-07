"""快照 presentation 投影（设计 5.3）：把展示事件表回放成前端可直用的轮次结构。

投影只是排版与可证明的过程；执行与业务结果仍以 LangGraph checkpoint 为准。
与 checkpoint 非原子提交——投影缺失或落后时，前端回退旧 messages 转换，
不据此重新执行业务。思考事件不落库，故不在此出现。
"""

import json
from typing import Any

PRESENTATION_VERSION = 1

_TERMINAL_DONE = "done"
_TERMINAL_ERROR = "error"


def build_presentation(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按 seq 回放事件，构造 {version, turns, last_seq, updated_at}。

    事件按 event_id 去重由存储层保证；乱序或缺失的事件跳过，
    不补造耗时与审批过程。无事件时返回 None。
    """
    if not events:
        return None
    turns: list[dict[str, Any]] = []
    # Storage returns insertion order, including runs created in the same second.
    # seq is local to a run; sorting all runs together would mix their answers.
    runs: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        runs.setdefault(event["run_id"], []).append(event)
    for run_events in runs.values():
        current = None
        for event in sorted(run_events, key=lambda e: e["seq"]):
            etype, payload = event["type"], event["payload"]
            if etype == "user_message":
                current = _new_turn(event)
                turns.append(current)
            elif current is not None:
                _apply(current, etype, payload)
    return {
        "version": PRESENTATION_VERSION,
        "turns": turns,
        "last_seq": events[-1]["seq"] if events else 0,
        "updated_at": events[-1]["occurred_at"] if events else None,
    }


def _new_turn(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": event["run_id"],
        "user_text": str(event["payload"].get("text") or ""),
        "user_index": event["payload"].get("user_index"),
        "blocks": [],
        "tools": {},
        "done": None,
        "error": None,
        "terminal": False,
    }


def _apply(turn: dict[str, Any], etype: str, payload: dict[str, Any]) -> None:
    if etype == "resume":
        turn.update(terminal=False, error=None, done=None)
        return
    if turn["terminal"]:
        return
    blocks, tools = turn["blocks"], turn["tools"]
    if etype == "step":
        return
    if etype == "delta":
        text = str(payload.get("text") or "")
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1]["text"] += text
        else:
            blocks.append({"type": "text", "text": text})
        return
    if etype == "tool_started":
        call_id = str(payload.get("id") or "")
        if not call_id or call_id in tools:
            return
        tools[call_id] = {
            "name": str(payload.get("name") or call_id),
            "arguments": str(payload.get("arguments") or ""),
            "status": "prepared",
            "finished": None,
            "approval": None,
            "approvalResolved": None,
        }
        blocks.append({"type": "tool", "callId": call_id})
        return
    if etype == "tool_executing":
        entity = tools.get(str(payload.get("id") or ""))
        if entity and entity["status"] != "denied":
            entity["status"] = "running"
        return
    if etype == "tool_finished":
        entity = tools.get(str(payload.get("id") or ""))
        if not entity:
            return
        entity["finished"] = payload
        if entity["status"] != "denied":
            entity["status"] = _result_status(payload)
        return
    if etype == "approval_pending":
        call_id = str(payload.get("call_id") or "")
        entity = tools.get(call_id)
        if entity is None:
            entity = tools[call_id] = {
                "name": str(payload.get("tool") or call_id),
                "arguments": str(payload.get("arguments") or ""),
                "status": "prepared",
                "finished": None,
                "approval": None,
                "approvalResolved": None,
            }
            blocks.append({"type": "tool", "callId": call_id})
        entity["status"] = "waiting_approval"
        entity["approval"] = payload
        entity["arguments"] = _arguments_json(payload.get("arguments"))
        return
    if etype == "approval_resolved":
        entity = tools.get(str(payload.get("call_id") or ""))
        if not entity:
            return
        entity["approvalResolved"] = payload
        entity["status"] = "prepared" if payload.get("approved") else "denied"
        return
    if etype == "done":
        turn["done"] = payload
        turn["terminal"] = True
        return
    if etype == "error":
        turn["error"] = str(payload.get("message") or "")
        turn["terminal"] = True


def _result_status(payload: dict[str, Any]) -> str:
    try:
        content = json.loads(payload.get("content") or "null")
    except (TypeError, ValueError):
        content = None
    if isinstance(content, dict) and content.get("approval") == "denied":
        return "denied"
    if not payload.get("ok") or (isinstance(content, dict) and content.get("error") is not None):
        return "failed"
    display = payload.get("display")
    if isinstance(display, dict) and display.get("version") == 1:
        outcome = display.get("outcome")
        if outcome in ("succeeded", "failed", "denied", "unknown"):
            return outcome
    result = content if isinstance(content, dict) else {}
    name = payload.get("name")
    if name in ("get_stock", "check_stock", "adjust_stock"):
        quantity = result.get("quantity", result.get("stock") if name != "adjust_stock" else None)
        return "succeeded" if type(quantity) is int and abs(quantity) <= 2**53 - 1 else "unknown"
    if name == "create_order":
        order_id = result.get("order_id")
        return "succeeded" if isinstance(order_id, str) and order_id else "unknown"
    return "succeeded"


def _arguments_json(value: Any) -> str:
    """审批载荷里的 arguments 是对象；恢复成与实时链路一致的 JSON 字符串。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
