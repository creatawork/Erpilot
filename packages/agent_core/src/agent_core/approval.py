"""审批门（HITL 前置，ADR-0005）：写工具执行前的硬约束拦截点。

两条不变量（第 2 周把 review 从同步回调升级为待审批事件时不动）：

1. 带 risk 的工具必须过门——guarded() 在工具层包装，loop 与组合层不产生
   第二个拦截点；"是否执行"不交给模型裁量，提示词是软约束，门是硬约束
2. 拒绝时回填结构化"未执行"结果——模型据此如实转述，绝不假装已执行

两种门形态（guarded 按 gate 能力自动分派，对调用方透明）：

- 同步回调门（review）：AutoDenyGate / AutoApproveGate / 脚本化策略门——
  决策立即返回，适合无真人的评测与自动化环境
- 挂起式事件门（suspend，M4 第 2 周）：StreamApprovalGate 把待审批请求
  打包成 ApprovalSuspended 控制流信号抛给 loop，loop 转成 ApprovalPending
  事件进事件流（trace / SSE / 前端审批卡片同源）并挂起等待；消费方拿到
  事件后经门 respond() 回填决策，批准则执行、拒绝则回填"未执行"。
  决策等待发生在 loop 层、工具超时之外——人的思考时间不该烧掉
  tool_timeout，超时只计量真正的执行段
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel

from agent_core.tools import Tool

# 风险等级（计划 §4 冻结口径的写侧两级；None = 只读放行）
RISK_BATCH_CONFIRM = "batch_confirm"  # 低风险：库存微调、上下架
RISK_SINGLE_CONFIRM = "single_confirm"  # 资金/单据：建单、取消

DENIED_NOTE = "该操作未经批准，未对系统产生任何变更；如实向用户说明，不得宣称已执行"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """一次待审批的写调用：工具名、风险等级与已校验的入参。

    client_token（T05，ADR-0008）：服务端在审批展示**前**生成的稳定幂等键，
    经 token_factory 注入并随请求透出——持久化与恢复都用它，执行段不得
    重新生成。为空表示调用方未接恢复层（CLI/同步评测门，行为不变）。
    """

    tool: str
    risk: str
    arguments: dict
    client_token: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


class ApprovalGate(Protocol):
    """同步回调门协议：review 立即返回决策（评测/自动化环境）。"""

    async def review(self, request: ApprovalRequest) -> ApprovalDecision: ...


class SuspendingGate(Protocol):
    """挂起式事件门协议（M4 第 2 周）：真人审批走的形态。

    suspend 由 guarded 的守门 handler 调用：门登记待审批请求并返回控制流
    信号（handler 将其抛出）；resume 封装了"决策到达后的续段"——批准则
    执行真 handler，拒绝则回填未执行结果，由 loop 在工具超时保护下调用。
    """

    def suspend(
        self,
        request: ApprovalRequest,
        resume: Callable[[ApprovalDecision], Awaitable[object]],
    ) -> "ApprovalSuspended": ...


class ApprovalSuspended(Exception):
    """守门工具等待人工决策的控制流信号：handler 抛出，loop 捕获后挂起会话。

    pending_id 由门生成，是消费方回填决策的凭据（gate.respond(pending_id)）；
    call_id 由 loop 捕获时回填，供事件流消费方与 ToolCallStarted 配对。
    """

    def __init__(
        self,
        request: ApprovalRequest,
        resume: Callable[[ApprovalDecision], Awaitable[object]],
        pending_id: str,
    ) -> None:
        super().__init__(request.tool)
        self.request = request
        self.resume = resume
        self.pending_id = pending_id
        self.call_id = ""
        self.decision: asyncio.Future[ApprovalDecision] | None = None
        self.cleanup: Callable[[], None] | None = None


def _denial_payload(decision: ApprovalDecision) -> str:
    """拒绝时回填给模型的结构化结果：明确"未执行"，防幻觉执行。"""
    reason = decision.reason or "需要人工审批"
    return json.dumps(
        {"approval": "denied", "message": f"操作未执行：{reason}", "note": DENIED_NOTE},
        ensure_ascii=False,
    )


def guarded(
    tool: Tool,
    gate: ApprovalGate | SuspendingGate,
    token_factory: Callable[[], str] | None = None,
) -> Tool:
    """给带 risk 的工具包上审批门；risk=None 的只读工具原样返回。

    token_factory（T05，ADR-0008）：提供时在**审批展示前**生成稳定幂等键——
    挂在请求上供组合层持久化，批准执行时注入同一参数对象，恢复路径复用
    原 token。不提供时行为与 M4 完全一致（同步评测门/CLI 不接恢复层）。

    同名同 schema——对模型透明，对 loop 透明：同步门在 handler 内先 review
    后执行；挂起门抛 ApprovalSuspended 由 loop 接管。拒绝一律返回未执行
    结果而不抛异常（挂起门的拒绝回填在 resume 闭包里，同样不抛）。
    """
    if tool.risk is None:
        return tool

    def _inject(args: BaseModel, token: str) -> None:
        if token and "client_token" in type(args).model_fields:
            args.client_token = token

    suspend = getattr(gate, "suspend", None)
    if suspend is not None:  # 挂起式事件门：等待决策发生在 loop 层

        async def suspending_handler(args: BaseModel) -> object:
            token = token_factory() if token_factory else ""
            request = ApprovalRequest(
                tool=tool.name, risk=tool.risk or "", arguments=args.model_dump(),
                client_token=token,
            )

            async def resume(decision: ApprovalDecision) -> object:
                if not decision.approved:
                    return _denial_payload(decision)
                _inject(args, token)  # 原 token 进执行段，恢复重放才对得上幂等表
                return await tool.handler(args)

            raise suspend(request, resume)

        return Tool(
            name=tool.name,
            description=tool.description,
            params_model=tool.params_model,
            handler=suspending_handler,
            risk=tool.risk,
            retry_safe=tool.retry_safe,
            schema_version=tool.schema_version,
        )

    async def handler(args: BaseModel) -> object:
        token = token_factory() if token_factory else ""
        request = ApprovalRequest(
            tool=tool.name, risk=tool.risk or "", arguments=args.model_dump(),
            client_token=token,
        )
        decision = await gate.review(request)  # type: ignore[attr-defined]
        if not decision.approved:
            return _denial_payload(decision)
        _inject(args, token)
        return await tool.handler(args)

    return Tool(
        name=tool.name,
        description=tool.description,
        params_model=tool.params_model,
        handler=handler,
        risk=tool.risk,
        retry_safe=tool.retry_safe,
        schema_version=tool.schema_version,
    )


class StreamApprovalGate:
    """挂起式人工审批门（M4 第 2 周）：待审批请求进事件流，决策由消费方回填。

    生命周期：suspend 登记 waiter 并把信号抛给 loop → loop 发 ApprovalPending
    事件后 await 信号上的 future（会话挂起）→ 消费方（CLI 确认 / API 审批
    卡片）拿 pending_id 调 respond() 落定决策 → loop 醒来按决策执行或回填。
    respond 对未知/已决的 pending_id 返回 False，不抛异常。
    """

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[ApprovalDecision]] = {}

    def suspend(
        self,
        request: ApprovalRequest,
        resume: Callable[[ApprovalDecision], Awaitable[object]],
    ) -> ApprovalSuspended:
        pending_id = uuid4().hex[:12]
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._waiters[pending_id] = future
        signal = ApprovalSuspended(request=request, resume=resume, pending_id=pending_id)
        signal.decision = future
        signal.cleanup = lambda: self.discard(pending_id)
        return signal

    def respond(self, pending_id: str, decision: ApprovalDecision) -> bool:
        """回填一次审批决策；返回 False 表示 pending_id 不存在或已决。"""
        future = self._waiters.get(pending_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        self._waiters.pop(pending_id, None)
        return True

    def discard(self, pending_id: str) -> None:
        """消费方放弃等待（会话取消/断连）时清理 waiter，防字典泄漏。"""
        future = self._waiters.pop(pending_id, None)
        if future is not None and not future.done():
            future.cancel()


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


def guard_tools(
    tools: list[Tool],
    gate: ApprovalGate | SuspendingGate,
    token_factory: Callable[[], str] | None = None,
) -> list[Tool]:
    """按工具遍历包门：只读原样、带 risk 的过门（组合层装配入口）。"""
    return [guarded(t, gate, token_factory) for t in tools]
