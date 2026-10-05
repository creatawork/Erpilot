"""声明式检查项 → 通过/失败（v1 全程序化判分；LLM-as-judge 后置，标注标准 §5）。"""

import re
from collections.abc import Sequence

from evals.model import EvalCase


def evaluate_case(
    case: EvalCase,
    *,
    tool_calls: Sequence[str],
    visible_text: str,
    steps: int,
    completed: bool,
) -> list[str]:
    """返回失败检查项的可读列表；空列表 = 通过。

    tool_calls 是工具调用名序列（按完成顺序、含重复）——顺序检查按
    "首次出现位置"判断，并行调用完成顺序抖动不影响先后语义。
    visible_text 是全部助手可见文本（过程消息 + 最终答复，按步骤顺序
    拼接）——这是 v2 判分范围（标注标准 §5.1）：过程澄清在 CLI/SSE/前端
    三端都实时展示给用户，只看最终答复会把真实澄清漏掉（edge-08 校准，
    2026-10-05）；工具参数与工具结果不进文本判分。
    """
    failed: list[str] = []
    if not completed:
        failed.append("completed: run 未正常结束（触发 max_steps 防护）")

    for name in case.expect_tools_all:
        if name not in tool_calls:
            failed.append(f"expect_tools_all: 未调用 {name}")
    if case.tools_in_order:
        positions = [_first(tool_calls, name) for name in case.expect_tools_all]
        if any(p is None for p in positions) or positions != sorted(positions):
            order = " → ".join(case.expect_tools_all)
            failed.append(f"tools_in_order: 期望顺序 {order}，实际 {tool_calls}")
    if case.expect_tools_any and not set(case.expect_tools_any) & set(tool_calls):
        failed.append(f"expect_tools_any: {'/'.join(case.expect_tools_any)} 均未调用")

    text = visible_text.casefold()
    for sub in case.must_mention:
        if sub.casefold() not in text:
            failed.append(f"must_mention: 回答未包含「{sub}」")
    if case.must_mention_any and not any(
        sub.casefold() in text for sub in case.must_mention_any
    ):
        failed.append(f"must_mention_any: {'/'.join(case.must_mention_any)} 均未出现")
    normalized_text = re.sub(r"[\s「」『』“”]", "", text)
    for group in case.must_mention_any_groups:
        if not any(sub.casefold() in normalized_text for sub in group):
            failed.append(f"must_mention_any_groups: {'/'.join(group)} 均未出现")
    for sub in case.must_not_mention:
        if sub.casefold() in text:
            failed.append(f"must_not_mention: 回答出现了「{sub}」")

    if case.max_steps is not None and steps > case.max_steps:
        failed.append(f"max_steps: {steps} 步超过上限 {case.max_steps}")
    return failed


def _first(seq: Sequence[str], value: str) -> int | None:
    for i, item in enumerate(seq):
        if item == value:
            return i
    return None


def case_id_of(node_id: str) -> str:
    """从 pytest nodeid 取 case id（test_evals_live.py::test_case[single-01]）。"""
    m = re.search(r"\[([a-z]+-\d+)\]$", node_id)
    return m.group(1) if m else node_id
