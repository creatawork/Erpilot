"""评测 case 与结果的数据契约（标注标准 §3 的机器可校验形态）。"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class CaseCategory(StrEnum):
    """四类 case（计划 §4 冻结口径；分派规则见标注标准 §2）。"""

    SINGLE = "single"  # 单工具
    MULTI = "multi"  # 多步
    EDGE = "edge"  # 边界
    ADVERSARIAL = "adversarial"  # 对抗


class EvalCase(BaseModel):
    """一条评测 case。

    检查项语义见 docs/eval-annotation-guide.md §5；question 与各检查项的
    子串里可写占位符（{on_sale_sku} 等，运行时由 context.resolve 解析）。
    """

    id: str = Field(description="类别前缀+序号，如 single-01；永不复用（标注标准 §6）")
    category: CaseCategory
    question: str = Field(description="用户原话；单轮独立提问")
    points: str = Field(description="考什么（人读）——写不出可复核检查项的场景不收")

    # ---- 检查项（全量默认检查 completed/无 run_error，不逐条写） ----
    expect_tools_all: list[str] = Field(
        default_factory=list, description="每个工具至少被调用一次"
    )
    tools_in_order: bool = Field(
        default=False, description="expect_tools_all 是否要求按列表顺序首次出现"
    )
    expect_tools_any: list[str] = Field(
        default_factory=list, description="至少调用了其中之一"
    )
    forbid_tools: list[str] = Field(default_factory=list, description="这些工具不得被调用")
    must_mention: list[str] = Field(
        default_factory=list,
        description="助手可见文本（过程消息+最终答复，v2 判分范围）须包含每条子串",
    )
    must_mention_any: list[str] = Field(
        default_factory=list,
        description="助手可见文本须包含至少一条子串（无结果/拒绝类回答的措辞不可枚举时用）",
    )
    must_mention_any_groups: list[list[str]] = Field(
        default_factory=list,
        description="每组都须至少出现一条子串；用于同时检查独立的说明义务",
    )
    must_not_mention: list[str] = Field(
        default_factory=list, description="助手可见文本不得包含任何一条子串（防编造/防泄露）"
    )
    must_not_match: list[str] = Field(
        default_factory=list, description="助手可见文本不得匹配的正则表达式"
    )
    max_steps: int | None = Field(default=None, description="步数上限（多步防绕路）")
    expect_successful_tools: list[str] = Field(default_factory=list)
    expect_error_codes: list[str] = Field(default_factory=list)
    successful_tool_counts: dict[str, int] = Field(default_factory=dict)
    state: "StateExpectation | None" = None

    def format_with(self, resolved: dict[str, str]) -> "EvalCase":
        """把 question 与检查项里的占位符替换为种子库真实值，返回新 case。"""
        return self.model_copy(
            update={
                "question": self.question.format(**resolved),
                "must_mention": [s.format(**resolved) for s in self.must_mention],
                "must_mention_any": [s.format(**resolved) for s in self.must_mention_any],
                "must_mention_any_groups": [
                    [s.format(**resolved) for s in group]
                    for group in self.must_mention_any_groups
                ],
                "must_not_mention": [s.format(**resolved) for s in self.must_not_mention],
                "state": self.state.model_copy(update={"sku": self.state.sku.format(**resolved)})
                if self.state else None,
            }
        )


class CaseResult(BaseModel):
    """一条 case 的运行结果（报告与回归对比的最小单元）。"""

    case_id: str
    category: CaseCategory
    passed: bool
    failed_checks: list[str] = Field(default_factory=list)
    steps: int = 0
    completed: bool = False
    tool_calls: list[str] = Field(
        default_factory=list, description="工具调用名序列（按完成顺序，含重复）"
    )
    total_tokens: int = 0
    cost: float | None = None
    duration_ms: float = 0.0
    attempts: int = Field(
        default=1, description="run 尝试次数；计量累计已返回 usage 的所有尝试"
    )
    error: str | None = Field(
        default=None, description="run 异常（APIError 等）；None = 正常结束"
    )
    tool_results: list["ToolResult"] = Field(default_factory=list)
    cost_complete: bool = True


class ToolResult(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any]
    content: Any
    ok: bool

    @property
    def succeeded(self) -> bool:
        return self.ok and not (
            isinstance(self.content, dict)
            and ("error" in self.content or self.content.get("approval") == "denied")
        )


class StateExpectation(BaseModel):
    kind: Literal["unchanged", "stock_delta", "product_status"]
    sku: str = ""
    delta: int = 0
    status: str = ""
