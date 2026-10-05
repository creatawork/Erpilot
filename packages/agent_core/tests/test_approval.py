"""审批门单测（M4 第 1 周，ADR-0005）：guarded 包装、拒绝回填、风险透传。"""

import json

from agent_core.approval import (
    RISK_SINGLE_CONFIRM,
    ApprovalDecision,
    ApprovalRequest,
    AutoApproveGate,
    AutoDenyGate,
    guard_tools,
    guarded,
)
from agent_core.tools import Tool, tool
from pydantic import BaseModel, Field


class SkuQuery(BaseModel):
    sku: str = Field(description="SKU")


async def _write_handler(params: SkuQuery) -> dict[str, str]:
    return {"executed": params.sku}


def _write_tool(risk: str | None = RISK_SINGLE_CONFIRM) -> Tool:
    return Tool(
        name="cancel_order",
        description="取消订单（需审批）",
        params_model=SkuQuery,
        handler=_write_handler,
        risk=risk,
    )


async def _run(tool: Tool, sku: str = "A1001") -> tuple[str, bool]:
    """模拟 loop 的调用路径：Pydantic 校验后执行 handler，返回 (content, ok)。"""
    args = tool.params_model.model_validate_json(json.dumps({"sku": sku}))
    result = await tool.handler(args)
    if isinstance(result, str):
        return result, True
    return json.dumps(result, ensure_ascii=False), True


def test_readonly_tool_passes_through_unguarded() -> None:
    """risk=None 的只读工具原样返回——门只拦写工具。"""
    readonly = _write_tool(risk=None)
    assert guarded(readonly, AutoDenyGate()) is readonly


def test_guarded_keeps_name_schema_and_risk() -> None:
    wrapped = guarded(_write_tool(), AutoDenyGate())
    assert wrapped.name == "cancel_order"
    assert wrapped.params_model is SkuQuery
    assert wrapped.description == "取消订单（需审批）"
    assert wrapped.risk == RISK_SINGLE_CONFIRM
    assert wrapped.openai_schema() == _write_tool().openai_schema()  # 对模型透明


async def test_denied_call_returns_structured_not_executed() -> None:
    """拒绝时回填未执行结果：模型必须能看出"没执行"，且不抛异常。"""
    wrapped = guarded(_write_tool(), AutoDenyGate("需要店长审批"))
    content, ok = await _run(wrapped)

    assert ok  # 拒绝不是工具错误，是治理结果
    payload = json.loads(content)
    assert payload["approval"] == "denied"
    assert "未执行" in payload["message"]
    assert "不得宣称已执行" in payload["note"]


async def test_approved_call_executes_underlying_handler() -> None:
    wrapped = guarded(_write_tool(), AutoApproveGate())
    content, _ = await _run(wrapped)
    assert json.loads(content) == {"executed": "A1001"}


async def test_gate_receives_tool_risk_and_arguments() -> None:
    seen: list[ApprovalRequest] = []

    class RecordingGate:
        async def review(self, request: ApprovalRequest) -> ApprovalDecision:
            seen.append(request)
            return ApprovalDecision(approved=False, reason="记一笔")

    wrapped = guarded(_write_tool(), RecordingGate())  # type: ignore[arg-type]
    await _run(wrapped, sku="B2002")

    assert len(seen) == 1
    assert seen[0].tool == "cancel_order"
    assert seen[0].risk == RISK_SINGLE_CONFIRM
    assert seen[0].arguments == {"sku": "B2002"}


def test_guard_tools_wraps_only_marked_tools() -> None:
    readonly = _write_tool(risk=None)
    writable = _write_tool()
    guarded_list = guard_tools([readonly, writable], AutoDenyGate())

    assert guarded_list[0] is readonly  # 只读不包
    assert guarded_list[1] is not writable  # 写工具被替换成包装版
    assert guarded_list[1].name == writable.name


def test_decorator_accepts_risk() -> None:
    @tool(name="w", description="写", params=SkuQuery, risk=RISK_SINGLE_CONFIRM)
    async def w(params: SkuQuery) -> dict[str, str]:
        return {}

    assert w.risk == RISK_SINGLE_CONFIRM


def test_auto_deny_gate_decision_shape() -> None:
    import asyncio

    gate = AutoDenyGate()
    decision = asyncio.run(
        gate.review(ApprovalRequest(tool="x", risk="r", arguments={}))
    )
    assert decision.approved is False
    assert decision.reason  # 拒绝必须带理由——模型转述时不能只说"被拒了"
