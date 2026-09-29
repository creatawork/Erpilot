"""上下文超长的截断/压缩策略 v1（计划 §6 第 3 周）。

两层防线，在 loop 每一步请求前就地应用：

1. 压缩：单条 tool 结果超长时截断（保留头尾 + 省略标记），防止单个工具输出撑爆上下文
2. 截断：整体估算超预算时，从最老的轮次开始整轮丢弃。轮 = 一个 user 消息及其后的
   assistant/tool 消息——保证 assistant tool_calls 与它的 tool 结果**成对**裁剪，
   不会留下没有父调用的 tool 消息（协议不允许）

system 消息与最近 keep_last_messages 条消息永远保留；发生丢弃时插入一条提示，
让模型知道前文被裁过。token 估算用 len/3 的粗略启发式（中英混合够用），
接 LiteLLM 后可换真 tokenizer。

注意：压缩是**就地**修改调用方的 messages（与 loop 的就地追加契约一致）；
需要完整历史做展示/追溯的场景，调用方自行留存副本。
"""

import json
from dataclasses import dataclass

from openai.types.chat import ChatCompletionMessageParam

OMIT_MARKER = "…[此处内容过长已截断]…"
DROPPED_NOTE = "注：为控制上下文长度，更早的对话轮次已被省略。"


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    max_tokens: int = 24_000
    keep_last_messages: int = 8
    max_tool_chars: int = 4_000


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + OMIT_MARKER + text[len(text) - tail :]


def _message_tokens(message: ChatCompletionMessageParam) -> int:
    text = message.get("content") if isinstance(message.get("content"), str) else ""
    if message.get("tool_calls"):
        text += json.dumps(message["tool_calls"], ensure_ascii=False)
    return max(1, len(text) // 3 + 1)


def estimate_tokens(messages: list[ChatCompletionMessageParam]) -> int:
    return sum(_message_tokens(m) for m in messages)


def _insert_dropped_note(messages: list[ChatCompletionMessageParam]) -> None:
    """在开头的 system 消息之后插入裁剪提示（最多一条，且不插在工具交换中间）。"""
    if any(m.get("content") == DROPPED_NOTE for m in messages):
        return
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        i += 1
    messages.insert(i, {"role": "system", "content": DROPPED_NOTE})


def compress_messages(
    messages: list[ChatCompletionMessageParam], policy: ContextPolicy
) -> None:
    """就地压缩消息历史到预算内（两层防线见模块 docstring）。"""
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            m["content"] = _truncate(m["content"], policy.max_tool_chars)
    if estimate_tokens(messages) <= policy.max_tokens:
        return

    keep = policy.keep_last_messages
    while estimate_tokens(messages) > policy.max_tokens and len(messages) > keep:
        # 可丢弃的轮首：位于保留窗口之外的最老 user 消息
        boundary = len(messages) - keep
        starts = [
            i for i, m in enumerate(messages) if m.get("role") == "user" and i < boundary
        ]
        if not starts:
            return
        start = starts[0]
        nxt = starts[1] if len(starts) > 1 else boundary
        end = min(nxt, boundary)
        # 裁剪边界不得落在工具交换中间：紧邻边界的 tool 结果必须连同父调用一起留/丢
        while end > start and messages[end].get("role") == "tool":
            end -= 1
        if end <= start:
            return  # 最老的可丢轮次也无法安全裁剪，放弃（v1 不做更细的条目级压缩）
        del messages[start:end]
        _insert_dropped_note(messages)
