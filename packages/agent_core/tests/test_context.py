"""上下文压缩单测：工具结果截断、整轮丢弃、工具交换不拆散、窗口保护。"""

from agent_core.context import (
    DROPPED_NOTE,
    OMIT_MARKER,
    ContextPolicy,
    compress_messages,
    estimate_tokens,
)


def test_tool_result_truncated_with_marker() -> None:
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "x" * 10_000}]

    compress_messages(messages, ContextPolicy(max_tool_chars=100))

    assert len(messages[0]["content"]) < 200
    assert OMIT_MARKER in messages[0]["content"]


def test_short_tool_result_untouched() -> None:
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "短结果"}]

    compress_messages(messages, ContextPolicy(max_tool_chars=100))

    assert messages[0]["content"] == "短结果"


def test_under_budget_noop() -> None:
    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    snapshot = [dict(m) for m in messages]

    compress_messages(messages, ContextPolicy(max_tokens=1000))

    assert messages == snapshot
    assert estimate_tokens(messages) <= 1000


def test_over_budget_drops_oldest_turns_keeps_system_and_recent() -> None:
    messages: list[dict] = [
        {"role": "system", "content": "你是掌柜助手"},
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "a" * 300},
        {"role": "user", "content": "q" * 300},
        {"role": "assistant", "content": "final"},
    ]

    compress_messages(messages, ContextPolicy(max_tokens=250, keep_last_messages=2))

    # system 保留，最老的轮次（user+assistant 约 202 tokens）整轮丢弃
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "system", "content": DROPPED_NOTE}
    assert not any(m["content"] == "u" * 300 for m in messages)
    assert messages[-2]["content"] == "q" * 300
    assert messages[-1]["content"] == "final"


def test_exchange_never_split() -> None:
    """裁剪边界落在工具交换中间时必须收拢：tool 结果不得失去父调用。"""
    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u" * 600},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "user", "content": "q" * 600},
        {"role": "assistant", "content": "final"},
    ]

    compress_messages(messages, ContextPolicy(max_tokens=250, keep_last_messages=3))

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "system", "content": DROPPED_NOTE}
    assert messages[2].get("tool_calls")  # 父调用保留
    assert messages[3]["tool_call_id"] == "c1"  # tool 结果与父调用都在
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            assert messages[i - 1].get("tool_calls"), "tool 消息失去了父调用"
