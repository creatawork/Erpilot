"""Small protocol helpers shared by graph nodes and tool execution."""

import json

from agent_core.llm import ToolCall, Usage


def error_payload(kind: str, message: str) -> str:
    return json.dumps({"error": {"type": kind, "message": message}}, ensure_ascii=False)


def merge_usage(a: Usage | None, b: Usage | None) -> Usage | None:
    if a is None:
        return b
    if b is None:
        return a
    return Usage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        total_tokens=a.total_tokens + b.total_tokens,
    )


def assistant_toolcall_message(text: str, tool_calls: list[ToolCall]) -> dict[str, object]:
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
