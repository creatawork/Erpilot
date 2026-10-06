"""R03–R06: kill an independent process in actual transaction/response windows."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agent_core.approval import StreamApprovalGate
from erp_store import ErpMutations, ErpRepository
from erp_store.db import make_engine
from erp_store.models import OrderStatus, ProductStatus
from erp_store.seed import seed_database
from erpilot_api.recovery import build_erp_recovery
from erpilot_api.run_store import RunStore
from fault_worker import mutate
from mcp_erp import build_agent_tools


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("products", "stocks", "orders", "order_items", "mutation_requests")}


@pytest.mark.parametrize("tool", [
    "adjust_stock", "create_order", "cancel_order", "set_product_status",
])
@pytest.mark.parametrize("stage", [
    "before_execute", "before_commit", "after_commit", "after_result",
])
def test_four_tools_converge_to_one_commit_after_process_kill(tmp_path, tool, stage):
    db = tmp_path / "erp.db"
    seed_database(db, n_products=60, n_orders=80)
    repo = ErpRepository(make_engine(db))
    sku = next(p.sku for p in repo.list_products(status=ProductStatus.ON_SALE, limit=60)
               if repo.get_stock(p.sku).quantity >= 10)
    arguments = {
        "adjust_stock": {"sku": sku, "delta": 3},
        "create_order": {"customer": "Crash Test", "items": [{"sku": sku, "quantity": 2}]},
        "cancel_order": {
            "order_id": repo.list_orders(status=OrderStatus.PENDING_PAYMENT)[0].order_id,
        },
        "set_product_status": {"sku": sku, "status": ProductStatus.OFF_SALE.value},
    }[tool]
    tools = build_agent_tools(db, writes=True, approval_gate=StreamApprovalGate())
    store = RunStore(tmp_path / "runs.db")
    run = store.create_run("s", "crash test", [], None)
    invocation = store.record_write_intent(
        run, "call", session_id="s", tool=tool,
        tool_schema_version=next(t.schema_version for t in tools if t.name == tool),
        arguments=arguments, client_token="crash-token", pending_id="p", ttl_seconds=1800,
    )
    store.decide_approval("p", True)
    store.set_invocation_status(invocation, "executing")
    before = snapshot(db)
    marker = tmp_path / "window.txt"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "db": str(db), "runs": str(store.path), "marker": str(marker), "stage": stage,
        "tool": tool, "arguments": arguments, "token": "crash-token", "invocation_id": invocation,
    }), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("fault_worker.py")), str(request)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            process.communicate(timeout=1) if process.poll() is not None else stage
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    assert process.returncode != 0  # 真实硬终止，不是正常退出或异常回滚替身。
    after_kill = snapshot(db)
    if stage in ("before_execute", "before_commit"):
        assert after_kill == before
    else:
        assert len(after_kill["mutation_requests"]) == 1
    recovery = build_erp_recovery(RunStore(store.path), str(db), tools)
    report = asyncio.run(recovery.recover_run(run))
    assert report["invocations"][0]["invocation_status"] == "succeeded"
    after = snapshot(db)
    assert len(after["mutation_requests"]) == 1
    assert after["mutation_requests"][0][0] == "crash-token"
    if stage in ("after_commit", "after_result"):
        assert after == after_kill
    # 业务幂等重放必须返回首次结果且完全不改变四表或幂等表。
    mutate(ErpMutations(make_engine(db)), tool, arguments, "crash-token")
    assert snapshot(db) == after
    asyncio.run(recovery.recover_run(run))
    assert snapshot(db) == after
    if tool == "adjust_stock":
        before_quantity = next(row[1] for row in before["stocks"] if row[0] == sku)
        assert repo.get_stock(sku).quantity == before_quantity + 3
    if tool == "create_order":
        assert len(after["orders"]) == len(before["orders"]) + 1
        assert len(after["order_items"]) == len(before["order_items"]) + 1
