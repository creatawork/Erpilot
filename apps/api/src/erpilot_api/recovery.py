"""恢复协调（T05/T06，ADR-0008 §3）：按 token 对账业务结果，修复运行状态。

职责边界——agent_core 不依赖 erp_store，本模块是应用组合层的"明确契约"：

- run_store（运行库）说"下一步可以做什么"；业务幂等表只说"是否成功提交"。
  两者跨库不要求原子，提交间隙靠 client_token 对账收口。
- 业务侧能力以两个协程接口注入：lookup（按 token 查幂等表，三态）与
  execute（原 token 直呼写工具一次）。build_erp_recovery 用 erp_store +
  mcp_erp 提供生产实现；测试注入假实现，不需要真实业务库。
- 对账顺序即恢复语义（恢复协议明细 §3 窗口表）：
  1. 已终态的 invocation 不动（拒绝零变更、成功结果不重放）；
  2. 业务已提交（token 有行且 request 一致）→ 回填首次成功结果，不重试——
     即使审批行仍显示 pending（决策落盘前进程崩溃：业务事实为准）；
  3. 同 token 异参（conflict）或工具版本不兼容 → 终止该恢复路径，
     原批准不迁移到新动作（R09）；
  4. 查询失败 → unknown，禁止任何重试（"无记录"只在查询成功时才成立）；
  5. 查询成功且无记录 → 仅当原批准有效、指纹一致、未过期时，用**原 token**
     重试一次；无记录本身绝不触发新 token 写入。
  6. 状态分类统一走 run_store.classify_tool_result：{"error": ...} 是确定性
     业务失败（failed），而超时/执行异常**不证明业务未提交**——标 unknown
     留给下次按 token 对账收口，绝不落终态。

T06 范围：adjust_stock 首条闭环（R03–R06 垂直验证）；run 的回答重建、
执行者租约与 resume API 归 T07/T08。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from erpilot_api.run_store import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_INVOCATION_STATUSES,
    RunStore,
    classify_tool_result,
    compute_fingerprint,
)


@dataclass(frozen=True, slots=True)
class TokenLookupOutcome:
    """lookup 接口的返回契约：found / not_found / conflict。

    查询失败（DB 错误、超时）由实现直接抛异常，协调器判 unknown——
    不设第四态，避免把"查不到"和"没查成"混在一个返回值里。
    """

    status: str
    result: Any = None


LookupFn = Callable[[str, str, dict], TokenLookupOutcome]
ExecuteFn = Callable[[str, dict, str], Awaitable[Any]]


class RecoveryError(RuntimeError):
    """恢复入口的确定性拒绝：run 不存在、已收口或状态不可恢复。"""


class RecoveryCoordinator:
    def __init__(
        self,
        run_store: RunStore,
        lookup: LookupFn,
        execute: ExecuteFn,
        *,
        schema_versions: dict[str, str] | None,
    ) -> None:
        self._store = run_store
        self.lookup = lookup  # 注入的对账契约，测试可替换/复用
        self.execute = execute
        # 工具签名版本注册表：提供时**失效闭**——注册表里没有的工具同样
        # 不得继承原批准（T05 验收 3）；显式传 None 才跳过校验（仅测试用）。
        self._schema_versions = schema_versions

    async def recover_run(self, run_id: str) -> dict[str, Any]:
        """恢复一个活动 run：逐 invocation 按 token 对账，返回处理报告。

        调用前 run 必须处于活动状态；恢复期间置 recovering（T08 在此之上
        加执行者租约，当前单进程先以状态位防重复入口）。
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise RecoveryError(f"run {run_id} 不存在")
        self._store.get_session(run["session_id"])
        cancelled = run["status"] == "cancelled"
        if run["status"] not in ACTIVE_RUN_STATUSES and not cancelled:
            raise RecoveryError(f"run {run_id} 状态为 {run['status']}，不可恢复")
        if not cancelled:
            self._store.set_run_status(run_id, "recovering")
        self._store.expire_approvals(run_id)
        actions = [await self._reconcile(inv) for inv in self._store.list_invocations(run_id)]
        pending = self._store.list_pending_approvals(run_id)
        if pending:  # 仍有待审批：交回审批生命周期（TTL 不延长，R02）
            self._store.set_run_status(run_id, "waiting_approval")
        return {"run_id": run_id, "invocations": actions, "pending": pending}

    async def _reconcile(self, invocation: dict[str, Any]) -> dict[str, Any]:
        invocation_id = invocation["invocation_id"]
        report: dict[str, Any] = {"invocation_id": invocation_id, "tool": invocation["tool"]}
        if invocation["status"] in TERMINAL_INVOCATION_STATUSES:
            expired = invocation["result"] == {"error": {"type": "approval_expired"}}
            return {**report, "invocation_status": invocation["status"],
                    "action": "approval_expired" if expired else "none"}

        # ---- 审批身份核验：按当前参数重算指纹——异参/异版本/篡改都不继承 ----
        approval = (
            self._store.get_approval(invocation["approval_pending_id"])
            if invocation["approval_pending_id"]
            else None
        )
        recomputed = compute_fingerprint(
            invocation["tool"], invocation["tool_schema_version"], invocation["arguments"]
        )
        if approval is not None and approval["arguments_fingerprint"] != recomputed:
            return self._terminate(
                invocation_id, report, "fingerprint_mismatch",
                "审批与调用参数不匹配，原批准不可迁移；请重新发起并审批",
            )

        # ---- 按 token 对账：业务已提交则回填，与审批行状态无关（W5） ----
        try:
            outcome = self.lookup(
                invocation["client_token"], invocation["tool"], invocation["arguments"]
            )
        except Exception as exc:  # noqa: BLE001 — 查询失败一律 unknown，禁止重试
            self._store.set_invocation_status(invocation_id, "unknown")
            return {
                **report, "invocation_status": "unknown", "action": "query_failed",
                "detail": str(exc),
            }
        if outcome.status == "found":
            self._store.set_invocation_status(
                invocation_id, "succeeded", {"committed_result": outcome.result}
            )
            return {**report, "invocation_status": "succeeded", "action": "backfilled"}
        if outcome.status == "conflict":
            return self._terminate(
                invocation_id, report, "idempotency_conflict",
                "幂等键已用于不同写请求：保留业务事实，原批准不可迁移，请人工核对后重新发起",
            )

        if self._store.get_run(invocation["run_id"])["status"] == "cancelled":
            return self._terminate(invocation_id, report, "run_cancelled", "任务已取消，不再执行")

        # ---- 无业务记录：仅持有效批准才允许原 token 重试一次（W3/W4） ----
        if approval is None:
            return self._terminate(
                invocation_id, report, "no_approval",
                "该调用没有持久化审批记录，未经审批不得执行恢复写入",
            )
        if approval["status"] == "pending":
            if self._expired(approval["expires_at"]):
                self._store.set_invocation_status(
                    invocation_id, "failed",
                    {"error": {"type": "approval_expired",
                               "message": "审批已过期，恢复入口不执行；请重新发起并审批"}},
                )
                return {
                    **report, "invocation_status": "failed", "action": "approval_expired",
                }
            # 未决：原样重建待审批（R01/R02），不执行、不改 TTL
            return {**report, "invocation_status": invocation["status"], "action": "wait_approval"}
        if approval["status"] != "approved":
            return self._terminate(
                invocation_id, report, f"approval_{approval['status']}",
                f"审批状态为 {approval['status']}，不执行恢复写入",
            )
        current_version = (
            self._schema_versions.get(invocation["tool"])
            if self._schema_versions is not None else None
        )
        if self._schema_versions is not None and (
            current_version is None or current_version != invocation["tool_schema_version"]
        ):
            return self._terminate(
                invocation_id, report, "tool_version_incompatible",
                "工具签名版本与审批时不一致（或工具已不在工具面），原批准不可继承；"
                "请重新发起并审批",
            )

        self._store.set_invocation_status(invocation_id, "executing")
        try:
            result = await self.execute(
                invocation["tool"], invocation["arguments"], invocation["client_token"]
            )
        except Exception as exc:  # noqa: BLE001 — 执行异常不证明业务未提交：
            # 事务可能已提交（ADR-0007），标可对账的 unknown，下次恢复按
            # token 查幂等表收口；落终态会让已提交的写永远无法回填。
            self._store.set_invocation_status(invocation_id, "unknown")
            return {
                **report, "invocation_status": "unknown", "action": "execute_failed",
                "detail": str(exc),
            }
        status, structured = classify_tool_result(result)
        self._store.set_invocation_status(invocation_id, status, structured)
        if status == "failed":
            return {**report, "invocation_status": "failed", "action": "business_error"}
        return {
            **report, "invocation_status": "succeeded",
            "action": "retried_with_original_token",
        }

    def _terminate(
        self, invocation_id: str, report: dict[str, Any], reason: str, message: str
    ) -> dict[str, Any]:
        self._store.set_invocation_status(
            invocation_id, "failed",
            {"error": {"type": "recovery_" + reason, "message": message}},
        )
        return {**report, "invocation_status": "failed", "action": reason}

    @staticmethod
    def _expired(expires_at: str) -> bool:
        """TTL 以落盘的绝对到期时间判定（UTC）——重连/重启不重置（R02）。"""
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)


def build_erp_recovery(
    run_store: RunStore, db_path: str, tools: list
) -> RecoveryCoordinator:
    """生产接线：erp_store 幂等表查询 + mcp_erp 原 token 执行（组合层装配）。

    tools 必填——签名版本注册表由它构建，缺了就静默跳过版本校验（失效开）。
    erp_store / mcp_erp 延迟导入——apps/api 在不接 ERP 恢复时保持可导入。
    """
    from erp_store import ErpMutations, MutationError
    from erp_store.db import make_engine
    from mcp_erp import execute_write_async

    db_path = Path(db_path)
    mutations = ErpMutations(make_engine(db_path))

    def lookup(token: str, tool: str, arguments: dict) -> TokenLookupOutcome:
        try:
            found = mutations.lookup_token(token)
        except MutationError:
            return TokenLookupOutcome("conflict")  # token 本身非法：按冲突终止
        if found.status != "found":
            return TokenLookupOutcome(found.status)
        if found.request != ErpMutations.canonical_request(tool, arguments):
            return TokenLookupOutcome("conflict", found.result)
        return TokenLookupOutcome("found", found.result)

    async def execute(tool: str, arguments: dict, client_token: str) -> Any:
        return await execute_write_async(db_path, tool, arguments, client_token)

    schema_versions = {
        t.name: t.schema_version for t in tools if t.schema_version is not None
    }
    return RecoveryCoordinator(run_store, lookup, execute, schema_versions=schema_versions)
