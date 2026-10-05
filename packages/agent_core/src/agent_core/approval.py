"""审批门（HITL 前置，ADR-0005）：写工具执行前的硬约束拦截点。

两条不变量（第 2–3 周把 review 从同步回调升级为待审批事件时不动）：

1. 带 risk 的工具必须过门——guarded() 在工具层包装，agent loop 与组合层
   零改动；"是否执行"不交给模型裁量，提示词是软约束，门是硬约束
2. 拒绝时回填结构化"未执行"结果——模型据此如实转述，绝不假装已执行
"""

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from agent_core.tools import Tool

# 风险等级（计划 §4 冻结口径的写侧两级；None = 只读放行）
RISK_BATCH_CONFIRM = "batch_confirm"  # 低风险：库存微调、上下架
RISK_SINGLE_CONFIRM = "single_confirm"  # 资金/单据：建单、取消

DENIED_NOTE = "该操作未经批准，未对系统产生任何变更；如实向用户说明，不得宣称已执行"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """一次待审批的写调用：工具名、风险等级与已校验的入参。"""

    tool: str
    risk: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


class ApprovalGate(Protocol):
    async def review(self, request: ApprovalRequest) -> ApprovalDecision: ...


def _denial_payload(decision: ApprovalDecision) -> str:
    """拒绝时回填给模型的结构化结果：明确"未执行"，防幻觉执行。"""
    reason = decision.reason or "需要人工审批"
    return json.dumps(
        {"approval": "denied", "message": f"操作未执行：{reason}", "note": DENIED_NOTE},
        ensure_ascii=False,
    )


def guarded(tool: Tool, gate: ApprovalGate) -> Tool:
    """给带 risk 的工具包上审批门；risk=None 的只读工具原样返回。

    同名同 schema——对模型透明，对 loop 透明：拦截发生在 handler 内，
    先 review 后执行；拒绝时返回未执行结果而不抛异常。
    """
    if tool.risk is None:
        return tool

    async def handler(args: BaseModel) -> object:
        request = ApprovalRequest(
            tool=tool.name, risk=tool.risk or "", arguments=args.model_dump()
        )
        decision = await gate.review(request)
        if not decision.approved:
            return _denial_payload(decision)
        return await tool.handler(args)

    return Tool(
        name=tool.name,
        description=tool.description,
        params_model=tool.params_model,
        handler=handler,
        risk=tool.risk,
    )


class AutoDenyGate:
    """拒绝一切写调用：评测环境没有真人（ADR-0005 决策 6）。"""

    def __init__(self, reason: str = "评测环境无审批人，写操作一律拒绝") -> None:
        self._reason = reason

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=False, reason=self._reason)


class AutoApproveGate:
    """放行一切写调用：单测/脚本化审批策略的对照基线，生产禁用。"""

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True)


def guard_tools(tools: list[Tool], gate: ApprovalGate) -> list[Tool]:
    """按工具遍历包门：只读原样、带 risk 的过门（组合层装配入口）。"""
    return [guarded(t, gate) for t in tools]
