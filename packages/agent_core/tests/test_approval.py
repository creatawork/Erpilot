"""审批门单测（M4 第 1-2 周，ADR-0005/0006）：guarded 包装、拒绝回填、风险透传。"""

import asyncio
import json

import pytest
from agent_core.approval import (
    RISK_SINGLE_CONFIRM,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalSuspended,
    AutoApproveGate,
    AutoDenyGate,
    StreamApprovalGate,
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
    decision = asyncio.run(gate.review(ApprovalRequest(tool="x", risk="r", arguments={})))
    assert decision.approved is False
    assert decision.reason  # 拒绝必须带理由——模型转述时不能只说"被拒了"


# ---- 挂起式事件门（M4 第 2 周，ADR-0006） ----


async def test_suspending_gate_handler_raises_signal() -> None:
    """挂起门的守门 handler 抛 ApprovalSuspended，而不是同步给决策。"""
    wrapped = guarded(_write_tool(), StreamApprovalGate())
    args = wrapped.params_model.model_validate_json('{"sku": "A1001"}')

    with pytest.raises(ApprovalSuspended) as exc_info:
        await wrapped.handler(args)

    signal = exc_info.value
    assert signal.request.tool == "cancel_order"
    assert signal.request.arguments == {"sku": "A1001"}
    assert signal.pending_id  # 门生成的回填凭据
    assert signal.call_id == ""  # loop 捕获时才回填
    assert signal.decision is not None and not signal.decision.done()


async def test_resume_executes_underlying_handler_on_approval() -> None:
    wrapped = guarded(_write_tool(), StreamApprovalGate())
    args = wrapped.params_model.model_validate_json('{"sku": "B2002"}')

    with pytest.raises(ApprovalSuspended) as exc_info:
        await wrapped.handler(args)
    signal = exc_info.value

    result = await signal.resume(ApprovalDecision(approved=True))
    assert result == {"executed": "B2002"}  # 批准 → 真执行


async def test_resume_returns_denial_payload_on_rejection() -> None:
    wrapped = guarded(_write_tool(), StreamApprovalGate())
    args = wrapped.params_model.model_validate_json('{"sku": "A1001"}')

    with pytest.raises(ApprovalSuspended) as exc_info:
        await wrapped.handler(args)
    signal = exc_info.value

    payload = await signal.resume(ApprovalDecision(approved=False, reason="店长不在"))
    assert json.loads(payload)["approval"] == "denied"
    assert "店长不在" in json.loads(payload)["message"]


async def test_stream_gate_respond_roundtrip() -> None:
    gate = StreamApprovalGate()

    async def fake_resume(decision: ApprovalDecision) -> object:
        return decision

    async def requester() -> ApprovalSuspended:
        request = ApprovalRequest(tool="w", risk="r", arguments={})
        signal = gate.suspend(request, fake_resume)
        return signal

    signal = await requester()
    assert not gate.respond("unknown-id", ApprovalDecision(approved=True))
    assert gate.respond(signal.pending_id, ApprovalDecision(approved=False, reason="拒绝"))
    assert not gate.respond(signal.pending_id, ApprovalDecision(approved=True))  # 已决
    assert await signal.decision == ApprovalDecision(approved=False, reason="拒绝")


def test_stream_gate_discard_cleans_waiter() -> None:
    gate = StreamApprovalGate()

    async def make() -> ApprovalSuspended:
        return gate.suspend(
            ApprovalRequest(tool="w", risk="r", arguments={}),
            lambda d: asyncio.sleep(0, result=d),
        )

    signal = asyncio.run(make())
    gate.discard(signal.pending_id)
    assert not gate.respond(signal.pending_id, ApprovalDecision(approved=True))


# ---- 稳定幂等键注入（T05，ADR-0008）：审批展示前生成，执行段复用 ----


async def test_token_factory_allocates_before_suspend_and_resume_reuses_it() -> None:
    """token 在挂起信号抛出前已生成并挂在请求上；批准执行注入同一 token。"""
    seen_tokens: list[str] = []

    async def write_handler(params: SkuQuery) -> dict[str, str]:
        seen_tokens.append(getattr(params, "client_token", ""))
        return {"executed": params.sku}

    write_tool = Tool(
        name="adjust_stock", description="调库存", params_model=SkuQuery,
        handler=write_handler, risk=RISK_SINGLE_CONFIRM,
    )
    # 参数模型带 client_token 字段（mcp_erp 写工具的形状）
    from pydantic import create_model

    TokenSku = create_model(
        "TokenSku", sku=(str, Field(description="SKU")),
        client_token=(str | None, Field(default=None)),
    )
    token_tool = Tool(
        name="adjust_stock", description="调库存", params_model=TokenSku,
        handler=write_handler, risk=RISK_SINGLE_CONFIRM,
    )

    gate = StreamApprovalGate()
    wrapped = guarded(token_tool, gate, token_factory=lambda: "token-t05")

    args = TokenSku(sku="A1001")
    with pytest.raises(ApprovalSuspended) as exc_info:
        await wrapped.handler(args)
    signal = exc_info.value
    assert signal.request.client_token == "token-t05"  # 展示前已分配
    assert args.client_token is None  # 执行前不落参数对象

    await signal.resume(ApprovalDecision(approved=True))
    assert seen_tokens == ["token-t05"]  # 原 token 进执行段

    # 无 factory：行为与 M4 一致，请求不带 token
    plain = guarded(write_tool, StreamApprovalGate())
    with pytest.raises(ApprovalSuspended) as exc_info:
        await plain.handler(SkuQuery(sku="A1001"))
    assert exc_info.value.request.client_token == ""


async def test_sync_gate_with_token_factory_injects_after_approval() -> None:
    """同步门路径：factory 提供时同样展示前分配、批准后注入（评测门不传则不变）。"""
    seen: list[ApprovalRequest] = []
    seen_tokens: list[str] = []

    async def write_handler(params: SkuQuery) -> dict[str, str]:
        seen_tokens.append(getattr(params, "client_token", ""))
        return {"executed": params.sku}

    from pydantic import create_model

    TokenSku = create_model(
        "TokenSku", sku=(str, Field(description="SKU")),
        client_token=(str | None, Field(default=None)),
    )
    token_tool = Tool(
        name="adjust_stock", description="调库存", params_model=TokenSku,
        handler=write_handler, risk=RISK_SINGLE_CONFIRM,
    )

    class RecordingGate:
        async def review(self, request: ApprovalRequest) -> ApprovalDecision:
            seen.append(request)
            return ApprovalDecision(approved=True)

    wrapped = guarded(token_tool, RecordingGate(), token_factory=lambda: "sync-token")  # type: ignore[arg-type]
    await wrapped.handler(TokenSku(sku="B2002"))

    assert seen[0].client_token == "sync-token"
    assert seen_tokens == ["sync-token"]
