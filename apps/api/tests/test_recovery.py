"""T05/T06：写调用身份持久化 + 按 token 对账的恢复闭环（ADR-0008，R01–R06）。

live 路径（ChatService → 门 → 桥 → 真库）验证 W1 落盘与执行 token 同源；
重启场景从同一持久化入口构造检查点状态，用独立 RunStore 实例打开，
验证恢复不依赖内存闭包。所有场景使用独立临时库（业务库 + 运行库）。
"""

import asyncio
import json
import sqlite3

import pytest
from agent_core.approval import ApprovalDecision, StreamApprovalGate, guarded
from agent_core.loop import LoopConfig, ToolRetryPolicy
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.tools import Tool
from erp_store import ErpMutations, ErpRepository
from erp_store.db import make_engine
from erp_store.seed import seed_database
from erpilot_api.recovery import (
    RecoveryCoordinator,
    RecoveryError,
    TokenLookupOutcome,
    build_erp_recovery,
)
from erpilot_api.run_store import RunStore, SchemaVersionError
from erpilot_api.service import ChatService
from mcp_erp import build_agent_tools, build_agent_tools_async

DELTA = 3
TOKEN = "token-adjust-1"
PENDING = "pend-t05"


@pytest.fixture
def erp(tmp_path):
    path = tmp_path / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    return path, ErpRepository(make_engine(path))


@pytest.fixture
def sku(erp) -> str:
    return _current_sku(erp)


def _current_sku(erp) -> str:
    _, repo = erp
    return next(p.sku for p in repo.list_products(limit=60)
                if repo.get_stock(p.sku).quantity >= 5)


def _tools(erp):
    gate = StreamApprovalGate()
    tools = build_agent_tools(
        erp[0], writes=True, approval_gate=gate, token_factory=lambda: TOKEN,
    )
    return tools, gate


def _service(erp, tmp_path, tools, gate, requests):
    sku = _current_sku(erp)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "库存已调整"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks(
            "call_adj", "adjust_stock", json.dumps({"sku": sku, "delta": DELTA}),
        ))

    return ChatService(
        client_factory=lambda: make_client(handler),
        model="glm-5.3-flash",
        trace_dir=tmp_path,
        tools=tools,
        approval_gate=gate,
        run_store=RunStore(tmp_path / "runs.db"),
    )


def _adjust_schema_version(tools) -> str:
    return next(t.schema_version for t in tools if t.name == "adjust_stock")


def _craft_w1(erp, tmp_path, tools, *, ttl_seconds=1800, pending_id=PENDING):
    """注入 W1 检查点（与 live 路径同一入口）：run + 展示前的写意图 + 待审批。"""
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "入库一批", [], None)
    invocation_id = store.record_write_intent(
        run_id, "call_adj", session_id="s1", tool="adjust_stock",
        tool_schema_version=_adjust_schema_version(tools),
        arguments={"sku": _current_sku(erp), "delta": DELTA},
        client_token=TOKEN, pending_id=pending_id, ttl_seconds=ttl_seconds,
    )
    return store, run_id, invocation_id


def _recovered(erp, tmp_path, tools, run_id) -> dict:
    coordinator = build_erp_recovery(RunStore(tmp_path / "runs.db"), str(erp[0]), tools)
    return asyncio.run(coordinator.recover_run(run_id))


def _business_snapshot(erp) -> dict:
    with sqlite3.connect(erp[0]) as conn:
        return {
            t: conn.execute(f"SELECT * FROM {t}").fetchall()  # noqa: S608 — 测试快照
            for t in ("products", "stocks", "orders", "order_items")
        }


def _mutation_rows(erp) -> list[tuple[str, str]]:
    with sqlite3.connect(erp[0]) as conn:
        return conn.execute(
            "SELECT client_token, request FROM mutation_requests"
        ).fetchall()


# ---- T05：审批展示前持久化写调用身份（live 路径，W1/W6） ----


async def test_write_intent_persisted_before_pending_event_and_reused_on_execution(
    erp, tmp_path, sku,
):
    gate = StreamApprovalGate()
    tools = await build_agent_tools_async(
        erp[0], writes=True, approval_gate=gate, token_factory=lambda: TOKEN,
    )
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    requests: list = []
    service = _service(erp, tmp_path, tools, gate, requests)

    events = []
    async for event in service.stream_run("s1", "入库一批"):
        if type(event).__name__ == "ApprovalPending":
            # 展示前：意图已落盘，业务未动
            invocation = service._run_store.get_invocation_by_token(TOKEN)
            assert invocation["call_id"] == "call_adj"
            assert invocation["tool"] == "adjust_stock"
            assert invocation["arguments"] == {"sku": sku, "delta": DELTA}
            assert invocation["status"] == "waiting_approval"
            assert len(invocation["arguments_fingerprint"]) == 64
            approval = service._run_store.get_approval(event.pending_id)
            assert approval["status"] == "pending"
            assert approval["arguments_fingerprint"] == invocation["arguments_fingerprint"]
            assert _mutation_rows(erp) == []  # 首次调用写 handler 之前
            assert repo.get_stock(sku).quantity == before
            assert service.respond_approval(event.pending_id, ApprovalDecision(approved=True))
        events.append(event)

    assert type(events[-1]).__name__ == "LoopEnd"
    invocation = service._run_store.get_invocation_by_token(TOKEN)
    assert invocation["status"] == "succeeded"  # W6：结果随 ToolCallFinished 落盘
    assert invocation["result"]["quantity"] == before + DELTA
    assert [t for t, _ in _mutation_rows(erp)] == [TOKEN]  # 执行用的就是持久化的 token
    assert repo.get_stock(sku).quantity == before + DELTA


def test_pending_approval_rebuilds_from_data_after_restart(erp, tmp_path):
    tools, _ = _tools(erp)
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)

    reopened = RunStore(tmp_path / "runs.db")  # 重启：全新实例从数据重建
    assert reopened.get_run(run_id)["status"] == "waiting_approval"
    pending = reopened.list_pending_approvals(run_id)
    assert len(pending) == 1
    card = pending[0]
    assert card["pending_id"] == PENDING
    assert card["client_token"] == TOKEN
    assert card["call_id"] == "call_adj"
    assert card["tool"] == "adjust_stock"
    assert card["arguments"] == {"sku": _current_sku(erp), "delta": DELTA}
    assert card["expected_version"] == 1
    assert card["expires_at"] == store.list_pending_approvals(run_id)[0]["expires_at"]


# ---- T06：按 token 对账（R03–R06，adjust_stock 垂直闭环） ----


def test_approved_before_restart_retries_once_with_original_token(erp, tmp_path, sku):
    """R03：决策落盘后、执行前重启——原 token 一次生效，二次恢复不重复写。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    assert store.record_approval_decision(PENDING, True)["status"] == "approved"

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "retried_with_original_token"
    assert report["invocations"][0]["invocation_status"] == "succeeded"
    assert repo.get_stock(sku).quantity == before + DELTA
    assert [t for t, _ in _mutation_rows(erp)] == [TOKEN]

    # 再次恢复：invocation 已终态 → 不动（结果已在首次恢复落盘），库存只变一次
    report2 = _recovered(erp, tmp_path, tools, run_id)
    assert report2["invocations"][0]["action"] == "none"
    assert report2["invocations"][0]["invocation_status"] == "succeeded"
    assert repo.get_stock(sku).quantity == before + DELTA


def test_committed_result_backfilled_when_response_was_lost(erp, tmp_path, sku):
    """R05/W5：业务已提交、响应丢失重启——按 token 取回首次结果，不重放。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, invocation_id = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)
    ErpMutations(make_engine(erp[0])).adjust_stock(
        sku, DELTA, client_token=TOKEN
    )  # 模拟：执行已提交，进程在结果落盘前中断
    store.set_invocation_status(invocation_id, "executing")

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "backfilled"
    reopened = RunStore(tmp_path / "runs.db")
    assert reopened.get_invocation(invocation_id)["status"] == "succeeded"
    assert reopened.get_invocation(invocation_id)["result"]["committed_result"] == [
        sku, before + DELTA
    ]
    assert repo.get_stock(sku).quantity == before + DELTA  # 只变更一次
    assert len(_mutation_rows(erp)) == 1


def test_query_failure_marks_unknown_and_forbids_retry(erp, tmp_path, sku):
    """R09：对账查询失败 → unknown，展示"结果待核对"，禁止换 token 重跑。"""
    tools, _ = _tools(erp)
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)

    def broken_lookup(token, tool, arguments):
        raise RuntimeError("业务库暂时不可用")

    async def never_execute(tool, arguments, client_token):
        raise AssertionError("unknown 路径不得触发执行")

    coordinator = RecoveryCoordinator(
        RunStore(tmp_path / "runs.db"), broken_lookup, never_execute,
        schema_versions={t.name: t.schema_version for t in tools},
    )
    report = asyncio.run(coordinator.recover_run(run_id))
    assert report["invocations"][0]["invocation_status"] == "unknown"
    assert report["invocations"][0]["action"] == "query_failed"
    assert _mutation_rows(erp) == []


@pytest.mark.parametrize("entry", ["manager", "coordinator"])
async def test_unsupported_session_schema_rejects_before_any_recovery_mutation(
    erp, tmp_path, entry,
):
    """Session validation must precede token reconciliation, lease and event writes."""
    from erpilot_api.execution import RunManager

    gate = StreamApprovalGate()
    tools = await build_agent_tools_async(erp[0], writes=True, approval_gate=gate)
    store, run_id, invocation_id = _craft_w1(erp, tmp_path, tools)
    store.decide_approval(PENDING, True)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE run_session SET schema_version=99")
    before = _business_snapshot(erp), _mutation_rows(erp)
    invocation = store.get_invocation(invocation_id)
    run = store.get_run(run_id)
    recovery = build_erp_recovery(store, str(erp[0]), tools)
    service = ChatService(client_factory=lambda: None, model="glm-5.3-flash",
                          trace_dir=tmp_path, tools=tools, approval_gate=gate, run_store=store)
    manager = RunManager(service, store, recovery)
    rejected = False
    try:
        if entry == "manager":
            await manager.resume(run_id)
            await manager.wait(run_id)
        else:
            await recovery.recover_run(run_id)
    except SchemaVersionError:
        rejected = True
    assert (_business_snapshot(erp), _mutation_rows(erp)) == before
    assert store.get_invocation(invocation_id) == invocation
    assert store.get_run(run_id) == run
    assert store.events_after(run_id) == []
    assert rejected


def test_denied_approval_recovery_leaves_all_business_tables_untouched(erp, tmp_path):
    """R03 拒绝面：拒绝后恢复——invocation=denied，四张业务表零变化。"""
    tools, _ = _tools(erp)
    before = _business_snapshot(erp)
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, False, "不需要入库")

    report = _recovered(erp, tmp_path, tools, run_id)
    # 拒绝在决策时即落盘（invocation=denied 终态）；恢复入口不再触碰业务
    assert report["invocations"][0]["invocation_status"] == "denied"
    assert report["invocations"][0]["action"] == "none"
    assert _business_snapshot(erp) == before
    assert _mutation_rows(erp) == []


def test_pending_approval_survives_recovery_without_execution(erp, tmp_path, sku):
    """R01/W2：待审批时重启——恢复入口只重建待审批，不执行、不延长 TTL。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    original_expiry = store.get_approval(PENDING)["expires_at"]

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "wait_approval"
    assert report["pending"][0]["pending_id"] == PENDING
    assert report["pending"][0]["expires_at"] == original_expiry  # TTL 不延长
    assert _mutation_rows(erp) == []
    assert repo.get_stock(sku).quantity == before
    assert RunStore(tmp_path / "runs.db").get_run(run_id)["status"] == "waiting_approval"


def test_expired_approval_is_not_executed(erp, tmp_path, sku):
    """R02：过期的审批不能借恢复入口执行，只能重新发起。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools, ttl_seconds=-10)

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "approval_expired"
    assert _mutation_rows(erp) == []
    assert repo.get_stock(sku).quantity == before


def test_tampered_arguments_do_not_inherit_approval(erp, tmp_path, sku):
    """T05 验收 3：异参数不得继承原批准——恢复时按参数重算指纹，替换即终止。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)
    # 模拟恢复时参数被替换：arguments 变了，审批行的指纹还是原参数的
    with sqlite3.connect(tmp_path / "runs.db") as conn:
        conn.execute(
            "UPDATE invocation SET arguments_json=:args WHERE client_token=:tok",
            {"args": json.dumps({"sku": sku, "delta": 99}, sort_keys=True,
                                ensure_ascii=False), "tok": TOKEN},
        )

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "fingerprint_mismatch"
    assert _mutation_rows(erp) == []
    assert repo.get_stock(sku).quantity == before


def test_incompatible_tool_version_aborts_retry(erp, tmp_path, sku):
    """T05 验收 3：审批时的工具签名版本与当前工具面不一致 → 不得继承原批准。

    用旧版本签名构造一致的 W1 检查点（指纹同源），恢复时当前注册表对不上。
    """
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "入库一批", [], None)
    store.record_write_intent(
        run_id, "call_adj", session_id="s1", tool="adjust_stock",
        tool_schema_version="old-schema",  # 审批时的版本 ≠ 当前工具面
        arguments={"sku": sku, "delta": DELTA},
        client_token=TOKEN, pending_id=PENDING, ttl_seconds=1800,
    )
    store.record_approval_decision(PENDING, True)

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "tool_version_incompatible"
    assert _mutation_rows(erp) == []
    assert repo.get_stock(sku).quantity == before


def test_same_token_with_different_arguments_ends_in_conflict(erp, tmp_path, sku):
    """R09：同 token 异参——保留业务事实，终止恢复路径，不做任何重试。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)
    ErpMutations(make_engine(erp[0])).adjust_stock(
        sku, 7, client_token=TOKEN
    )  # 该 token 已被另一组参数占用

    report = _recovered(erp, tmp_path, tools, run_id)
    assert report["invocations"][0]["action"] == "idempotency_conflict"
    assert report["invocations"][0]["invocation_status"] == "failed"
    assert len(_mutation_rows(erp)) == 1
    assert repo.get_stock(sku).quantity == before + 7  # 只有那次直接写入生效


def test_recovery_entry_rejects_unknown_and_ended_runs(erp, tmp_path):
    tools, _ = _tools(erp)
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, False)
    store.finish_run(run_id, [{"role": "assistant", "content": "done"}], "done")

    coordinator = build_erp_recovery(store, str(erp[0]), tools)
    with pytest.raises(RecoveryError, match="不存在"):
        asyncio.run(coordinator.recover_run("nope"))
    with pytest.raises(RecoveryError, match="不可恢复"):
        asyncio.run(coordinator.recover_run(run_id))


def test_lookup_outcome_contract_shapes():
    """对账契约三态显式：found/not_found/conflict——查询失败走异常，不占状态位。"""
    assert TokenLookupOutcome("found", {"k": 1}).status == "found"
    assert TokenLookupOutcome("not_found").result is None
    assert TokenLookupOutcome("conflict").result is None


# ---- 评审补强：live/恢复两条路径的状态分类一致性与可对账性 ----


async def test_live_business_error_marks_invocation_failed(erp, tmp_path, sku):
    """live 路径：批准后的写调用撞上确定性业务错误 → failed（非 succeeded）。"""
    gate = StreamApprovalGate()
    tools = await build_agent_tools_async(
        erp[0], writes=True, approval_gate=gate, token_factory=lambda: TOKEN,
    )
    repo = erp[1]
    before = repo.get_stock(sku).quantity

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "出库失败"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks(
            "call_adj", "adjust_stock",
            json.dumps({"sku": sku, "delta": -(before + 10)}),  # 必然 insufficient_stock
        ))

    service = ChatService(
        client_factory=lambda: make_client(handler), model="glm-5.3-flash",
        trace_dir=tmp_path, tools=tools, approval_gate=gate,
        run_store=RunStore(tmp_path / "runs.db"),
    )
    async for event in service.stream_run("s1", "出库一批"):
        if type(event).__name__ == "ApprovalPending":
            assert service.respond_approval(event.pending_id, ApprovalDecision(approved=True))

    invocation = RunStore(tmp_path / "runs.db").get_invocation_by_token(TOKEN)
    assert invocation["status"] == "failed"  # 与恢复路径的 business_error 同一口径
    assert invocation["result"]["error"]["code"] == "insufficient_stock"
    assert repo.get_stock(sku).quantity == before  # 业务零变化


def test_execute_exception_is_unknown_then_reconciles(erp, tmp_path, sku):
    """执行异常不证明业务未提交 → unknown；实际已提交时下次恢复能回填。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)

    working = build_erp_recovery(RunStore(tmp_path / "runs.db"), str(erp[0]), tools)

    async def explode(tool, arguments, client_token):
        raise ConnectionError("执行通道中断")

    coordinator = RecoveryCoordinator(
        RunStore(tmp_path / "runs.db"), working.lookup, explode,
        schema_versions={t.name: t.schema_version for t in tools},
    )
    report = asyncio.run(coordinator.recover_run(run_id))
    assert report["invocations"][0]["invocation_status"] == "unknown"
    assert report["invocations"][0]["action"] == "execute_failed"
    assert _mutation_rows(erp) == []

    # 事务其实在中断前提交了：下次恢复按 token 查到 → 回填，不重试
    committed = ErpMutations(make_engine(erp[0])).adjust_stock(
        sku, DELTA, client_token=TOKEN
    )
    report2 = _recovered(erp, tmp_path, tools, run_id)
    assert report2["invocations"][0]["action"] == "backfilled"
    reopened = RunStore(tmp_path / "runs.db")
    assert reopened.get_invocation_by_token(TOKEN)["status"] == "succeeded"
    assert reopened.get_invocation_by_token(TOKEN)["result"]["committed_result"] == list(committed)
    assert repo.get_stock(sku).quantity == before + DELTA
    assert len(_mutation_rows(erp)) == 1


def test_multi_invocation_run_reconciles_partially_and_rebuilds_pending(erp, tmp_path, sku):
    """推演 1：一个 run 两个写调用——A 已提交、B 待审批。恢复只回填 A，B 重建。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run("s1", "两笔调整", [], None)
    store.record_write_intent(
        run_id, "call_a", session_id="s1", tool="adjust_stock",
        tool_schema_version=_adjust_schema_version(tools),
        arguments={"sku": sku, "delta": DELTA},
        client_token="tok-a", pending_id="pend-a", ttl_seconds=1800,
    )
    store.record_write_intent(
        run_id, "call_b", session_id="s1", tool="adjust_stock",
        tool_schema_version=_adjust_schema_version(tools),
        arguments={"sku": sku, "delta": 1},
        client_token="tok-b", pending_id="pend-b", ttl_seconds=1800,
    )
    store.record_approval_decision("pend-a", True)
    ErpMutations(make_engine(erp[0])).adjust_stock(sku, DELTA, client_token="tok-a")
    store.set_invocation_status(
        store.get_invocation_by_token("tok-a")["invocation_id"], "executing"
    )  # A：提交后结果落盘前中断；B：尚未决策

    report = _recovered(erp, tmp_path, tools, run_id)
    by_call = {r["invocation_id"]: r for r in report["invocations"]}
    a = by_call[store.get_invocation_by_token("tok-a")["invocation_id"]]
    b = by_call[store.get_invocation_by_token("tok-b")["invocation_id"]]
    assert a["action"] == "backfilled" and a["invocation_status"] == "succeeded"
    assert b["action"] == "wait_approval" and b["invocation_status"] == "waiting_approval"
    assert [p["pending_id"] for p in report["pending"]] == ["pend-b"]
    assert repo.get_stock(sku).quantity == before + DELTA  # A 只生效一次，B 未执行
    assert [t for t, _ in _mutation_rows(erp)] == ["tok-a"]


def test_version_check_fails_closed_without_registry_entry(erp, tmp_path, sku):
    """签名版本注册表缺工具 = 不可继承（失效闭）；显式 None 才是跳过校验。"""
    tools, _ = _tools(erp)
    repo = erp[1]
    before = repo.get_stock(sku).quantity
    store, run_id, _ = _craft_w1(erp, tmp_path, tools)
    store.record_approval_decision(PENDING, True)

    async def never_execute(tool, arguments, client_token):
        raise AssertionError("注册表缺工具时不得执行恢复写入")

    coordinator = RecoveryCoordinator(
        RunStore(tmp_path / "runs.db"),
        lambda token, tool, args: TokenLookupOutcome("not_found"),
        never_execute,
        schema_versions={},  # 空注册表 ≠ 关闭校验
    )
    report = asyncio.run(coordinator.recover_run(run_id))
    assert report["invocations"][0]["action"] == "tool_version_incompatible"
    assert _mutation_rows(erp) == []
    assert repo.get_stock(sku).quantity == before


@pytest.mark.parametrize("answer_fails", [False, True])
async def test_live_lost_response_remains_recoverable_after_answer(
    erp, tmp_path, sku, answer_fails,
):
    """提交后响应丢失：回答完成或失败均不能封死 unknown 的原 token 对账。"""
    gate = StreamApprovalGate()
    discovered = await build_agent_tools_async(erp[0], writes=True, approval_gate=gate)
    original = next(t for t in discovered if t.name == "adjust_stock")
    before = erp[1].get_stock(sku).quantity

    async def lose_response(args):
        ErpMutations(make_engine(erp[0])).adjust_stock(
            args.sku, args.delta, client_token=args.client_token,
        )
        raise ConnectionError("response lost after commit")

    tools = [guarded(Tool(
        name=original.name, description=original.description, params_model=original.params_model,
        handler=lose_response, risk=original.risk, retry_safe=True,
        schema_version=original.schema_version,
    ), gate, token_factory=lambda: TOKEN)]

    def handler(request):
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            if answer_fails:
                raise RuntimeError("answer unavailable")
            return sse_response([
                chunk(delta={"content": "结果待核对"}), chunk(usage=USAGE),
            ])
        return sse_response(tool_call_chunks(
            "call_adj", "adjust_stock", json.dumps({"sku": sku, "delta": DELTA}),
        ))

    store = RunStore(tmp_path / "runs.db")
    service = ChatService(
        client_factory=lambda: make_client(handler), model="glm-5.3-flash",
        trace_dir=tmp_path, tools=tools, approval_gate=gate, run_store=store,
        loop_config=LoopConfig(retry=ToolRetryPolicy(backoff=0)),
    )
    prior = list(service._session_messages("s1"))

    async def consume():
        async for event in service.stream_run("s1", "入库一批"):
            if type(event).__name__ == "ApprovalPending":
                assert service.respond_approval(event.pending_id, ApprovalDecision(approved=True))

    if answer_fails:
        with pytest.raises(RuntimeError, match="answer unavailable"):
            await consume()
    else:
        await consume()
    reopened = RunStore(store.path)
    invocation = reopened.get_invocation_by_token(TOKEN)
    run_id = invocation["run_id"]
    assert invocation["status"] == "unknown"
    assert reopened.get_run(run_id)["status"] == "recovering"
    assert reopened.get_run(run_id)["final_answer"] is None
    assert reopened.get_session("s1")["active_run_id"] == run_id
    assert reopened.get_session("s1")["history"] == prior
    assert service._session_messages("s1") == prior
    assert erp[1].get_stock(sku).quantity == before + DELTA
    assert [t for t, _ in _mutation_rows(erp)] == [TOKEN]

    coordinator = build_erp_recovery(reopened, str(erp[0]), tools)
    report = await coordinator.recover_run(run_id)
    assert report["invocations"][0]["action"] == "backfilled"
    assert reopened.get_invocation_by_token(TOKEN)["result"]["committed_result"] == [
        sku, before + DELTA,
    ]
    assert (await coordinator.recover_run(run_id))["invocations"][0]["action"] == "none"
    assert erp[1].get_stock(sku).quantity == before + DELTA
    assert [t for t, _ in _mutation_rows(erp)] == [TOKEN]


@pytest.mark.parametrize("committed", [False, True])
async def test_restart_resume_restores_original_approval_and_answer_without_replanning(
    erp, tmp_path, sku, committed,
):
    from erpilot_api.execution import RunManager

    gate = StreamApprovalGate()
    tools = await build_agent_tools_async(
        erp[0], writes=True, approval_gate=gate, token_factory=lambda: TOKEN,
    )
    store, run_id, invocation_id = _craft_w1(erp, tmp_path, tools)
    store.checkpoint_run(run_id, [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "之前的操作"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "historical", "type": "function", "function": {
                "name": "adjust_stock", "arguments": json.dumps({"sku": sku, "delta": 99}),
            },
        }]},
        {"role": "tool", "tool_call_id": "historical", "content": '{"historical":true}'},
        {"role": "assistant", "content": "已完成历史操作"},
        {"role": "user", "content": "入库一批"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_adj", "type": "function", "function": {
                "name": "adjust_stock", "arguments": json.dumps({"sku": sku, "delta": DELTA}),
            },
        }]},
    ])
    before = erp[1].get_stock(sku).quantity
    if committed:
        store.record_approval_decision(PENDING, True)
        ErpMutations(make_engine(erp[0])).adjust_stock(sku, DELTA, client_token=TOKEN)
        store.set_invocation_status(invocation_id, "executing")
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        result = json.loads(next(m["content"] for m in body["messages"]
                                 if m.get("tool_call_id") == "call_adj"))
        assert result["quantity"] == before + DELTA
        assert all(t["function"]["name"] != "adjust_stock" for t in body.get("tools", []))
        return sse_response([chunk(delta={"content": "库存已调整"}), chunk(usage=USAGE)])
    reopened = RunStore(store.path)
    service = ChatService(client_factory=lambda: make_client(handler), model="glm-5.3-flash",
                          trace_dir=tmp_path, tools=tools, approval_gate=gate, run_store=reopened)
    runs = RunManager(service, reopened, build_erp_recovery(reopened, str(erp[0]), tools))
    await runs.resume(run_id)
    if not committed:
        async with asyncio.timeout(5):
            while not any(e["event"] == "approval_pending" for e in reopened.events_after(run_id)):
                await asyncio.sleep(0.01)
        assert reopened.get_approval(PENDING)["status"] == "pending"
        assert service.respond_approval(PENDING, ApprovalDecision(True), expected_version=1)
    await runs.wait(run_id)
    assert reopened.get_run(run_id)["status"] == "completed"
    assert reopened.get_run(run_id)["final_answer"] == "库存已调整"
    assert len(requests) == 1
    assert erp[1].get_stock(sku).quantity == before + DELTA
    assert [token for token, _ in _mutation_rows(erp)] == [TOKEN]


async def test_resume_preserves_validation_failure_without_creating_write_intent(erp, tmp_path):
    from erpilot_api.execution import RunManager

    gate = StreamApprovalGate()
    tools = await build_agent_tools_async(erp[0], writes=True, approval_gate=gate)
    store = RunStore(tmp_path / "runs.db")
    run = store.create_run("s", "invalid input", [], None)
    failure = '{"error":{"type":"validation_error"}}'
    store.checkpoint_run(run, [
        {"role": "user", "content": "invalid input"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "bad", "type": "function", "function": {
                "name": "adjust_stock", "arguments": '{"delta":"invalid"}',
            },
        }]},
    ])
    store.append_event(run, "tool_finished", {
        "id": "bad", "name": "adjust_stock", "content": failure, "ok": False,
    })
    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][-1]["content"] == failure
        return sse_response([chunk(delta={"content": "输入有误"})])
    service = ChatService(client_factory=lambda: make_client(handler), model="glm-5.3-flash",
                          trace_dir=tmp_path, tools=tools, approval_gate=gate, run_store=store)
    runs = RunManager(service, store, build_erp_recovery(store, str(erp[0]), tools))
    await runs.resume(run)
    await runs.wait(run)
    assert store.get_run(run)["status"] == "completed"
    assert store.list_invocations(run) == []
    assert _mutation_rows(erp) == []
