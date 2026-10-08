"""Hard process exits around the real graph checkpoint and ERP mutation boundary."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from erp_store.db import MutationRequestRow, make_engine
from erp_store.models import OrderStatus, ProductStatus
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from sqlalchemy import select
from sqlalchemy.orm import Session

_WORKER = Path(__file__).with_name("fault_worker.py")


def _child(tmp_path, config, *, expected):
    config_path = tmp_path / f"worker-{uuid4().hex}.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_WORKER), str(config_path)],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    detail = result.stdout + result.stderr
    marker = Path(config["marker"])
    if marker.exists():
        detail += "\n" + marker.read_text(encoding="utf-8")
    assert result.returncode == expected, detail
    return json.loads(Path(config["marker"]).read_text(encoding="utf-8"))


def test_fault_worker_smoke_is_a_real_hard_process_exit():
    result = subprocess.run(
        [sys.executable, str(_WORKER), "smoke"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 79


def _operation_fixture(db_path: Path, operation: str):
    seed_database(db_path, n_products=60, n_orders=80)
    engine = make_engine(db_path)
    repo = ErpRepository(engine)
    product = repo.list_products(limit=1)[0]
    if operation == "create_order":
        arguments = {
            "customer": "R05 恢复验证客户",
            "items": [{"sku": product.sku, "quantity": 1}],
        }
        before = (repo.get_stock(product.sku).quantity, len(repo.list_orders(limit=1000)))
    elif operation == "cancel_order":
        order = repo.list_orders(status=OrderStatus.PENDING_PAYMENT, limit=1)[0]
        arguments = {"order_id": order.order_id}
        before = (repo.get_order(order.order_id).status,)
    elif operation == "adjust_stock":
        arguments = {"sku": product.sku, "delta": 1}
        before = (repo.get_stock(product.sku).quantity,)
    else:
        target = (
            ProductStatus.OFF_SALE
            if product.status == ProductStatus.ON_SALE
            else ProductStatus.ON_SALE
        )
        arguments = {"sku": product.sku, "status": target.value}
        before = (product.status,)
    engine.dispose()
    return arguments, before


def _business_snapshot(db_path: Path, operation: str, arguments: dict):
    engine = make_engine(db_path)
    repo = ErpRepository(engine)
    if operation == "create_order":
        result = (repo.get_stock(arguments["items"][0]["sku"]).quantity,
                  len(repo.list_orders(limit=1000)))
    elif operation == "cancel_order":
        result = (repo.get_order(arguments["order_id"]).status,)
    elif operation == "adjust_stock":
        result = (repo.get_stock(arguments["sku"]).quantity,)
    else:
        result = (repo.get_product(arguments["sku"]).status,)
    engine.dispose()
    return result


@pytest.mark.parametrize(
    ("operation", "crash", "exit_code", "expected_calls"),
    [
        (operation, crash, code, calls)
        for operation in ("create_order", "cancel_order", "adjust_stock", "set_product_status")
        for crash, code, calls in (
            ("after_approval_checkpoint", 71, 1),
            ("before_commit", 72, 2),
            ("after_commit", 73, 1),
            ("before_final_answer", 74, 1),
        )
    ],
)
async def test_r03_to_r06_hard_crash_matrix(
    tmp_path, isolated_postgres_url, operation, crash, exit_code, expected_calls
):
    db_path = tmp_path / "erp-isolated.sqlite"
    arguments, before = _operation_fixture(db_path, operation)
    thread_id = f"recovery-{uuid4().hex}"
    marker = tmp_path / "marker.json"
    calls = tmp_path / "handler-calls.log"
    common = {
        "postgres_url": isolated_postgres_url,
        "erp_db": str(db_path),
        "thread_id": thread_id,
        "operation": operation,
        "arguments": arguments,
        "marker": str(marker),
        "calls": str(calls),
    }
    pending = _child(tmp_path, {**common, "stage": "prepare"}, expected=70)
    # For R03, this is after approval was durably resumed and before the
    # reconciler can invoke the ERP handler. Other cases enter the handler.
    _child(
        tmp_path,
        {**common, **pending, "stage": "resume", "crash": crash},
        expected=exit_code,
    )
    after_crash = _business_snapshot(db_path, operation, arguments)
    if crash in ("after_approval_checkpoint", "before_commit"):
        assert after_crash == before
    else:
        assert after_crash != before

    recovered = _child(
        tmp_path,
        {**common, **pending, "stage": "continue", "crash": None},
        expected=0,
    )
    assert recovered["completed"] is True
    assert len(calls.read_text(encoding="utf-8").splitlines()) == expected_calls

    engine = make_engine(db_path)
    with Session(engine) as session:
        mutation_rows = list(session.scalars(select(MutationRequestRow)))
    engine.dispose()
    assert len(mutation_rows) == 1, recovered
    assert _business_snapshot(db_path, operation, arguments) != before


async def test_r01_process_exit_after_approval_checkpoint_before_display(
    tmp_path, isolated_postgres_url
):
    db_path = tmp_path / "erp-r01.sqlite"
    arguments, before = _operation_fixture(db_path, "adjust_stock")
    marker = tmp_path / "marker.json"
    calls = tmp_path / "handler-calls.log"
    common = {
        "postgres_url": isolated_postgres_url,
        "erp_db": str(db_path),
        "thread_id": f"r01-{uuid4().hex}",
        "operation": "adjust_stock",
        "arguments": arguments,
        "marker": str(marker),
        "calls": str(calls),
    }
    pending = _child(tmp_path, {**common, "stage": "prepare"}, expected=70)
    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.llm import StreamEnd, ToolCall
    from agent_core.tools import Tool
    from erpilot_api.checkpoint import open_checkpointer
    from pydantic import BaseModel

    class Params(BaseModel):
        sku: str
        delta: int
        client_token: str | None = None

    async def handler(_args):
        raise AssertionError("pending approval must not invoke handler")

    class Client:
        async def stream_chat(self, messages, tools=None):
            yield ToolCall("call-write", "adjust_stock", json.dumps(arguments))
            yield StreamEnd()

    async with open_checkpointer(isolated_postgres_url) as saver:
        runtime = LangGraphRuntime(
            Client(), [Tool("adjust_stock", "write", Params, handler, risk="confirm")],
            checkpointer=saver, approval_enabled=True,
        )
        snapshot = await runtime.get_state(common["thread_id"])
        checkpoint_pending = [i.value for task in snapshot.tasks for i in task.interrupts][0]
        assert checkpoint_pending["pending_id"] == pending["pending_id"]
        assert checkpoint_pending["arguments"] == pending["arguments"]
        assert checkpoint_pending["expires_at"] == pending["expires_at"]
    assert _business_snapshot(db_path, "adjust_stock", arguments) == before
    assert not calls.exists()


async def test_r02_restart_does_not_extend_approval_expiry(tmp_path, isolated_postgres_url):
    db_path = tmp_path / "erp-r02.sqlite"
    arguments, before = _operation_fixture(db_path, "adjust_stock")
    marker = tmp_path / "marker.json"
    calls = tmp_path / "handler-calls.log"
    common = {
        "postgres_url": isolated_postgres_url,
        "erp_db": str(db_path),
        "thread_id": f"r02-{uuid4().hex}",
        "operation": "adjust_stock",
        "arguments": arguments,
        "marker": str(marker),
        "calls": str(calls),
        "approval_ttl_seconds": 1,
    }
    pending = _child(tmp_path, {**common, "stage": "prepare"}, expected=70)
    time.sleep(1.05)
    _child(
        tmp_path,
        {**common, **pending, "stage": "continue", "crash": None},
        expected=0,
    )
    assert not calls.exists()
    assert _business_snapshot(db_path, "adjust_stock", arguments) == before

    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.llm import StreamEnd, ToolCall
    from agent_core.tools import Tool
    from erpilot_api.checkpoint import open_checkpointer
    from pydantic import BaseModel

    class Params(BaseModel):
        sku: str
        delta: int
        client_token: str | None = None

    async def handler(_args):
        raise AssertionError("expired approval must not invoke handler")

    class Client:
        async def stream_chat(self, messages, tools=None):
            yield ToolCall("call-write", "adjust_stock", json.dumps(arguments))
            yield StreamEnd()

    async with open_checkpointer(isolated_postgres_url) as saver:
        runtime = LangGraphRuntime(
            Client(), [Tool("adjust_stock", "write", Params, handler, risk="confirm")],
            checkpointer=saver, approval_enabled=True,
        )
        snapshot = await runtime.get_state(common["thread_id"])
        assert snapshot.values["tool_history"][0]["invocation_status"] == "expired"


# R07/R08 use the API lock/late-decision tests in test_api.py; R09 is pinned by
# test_graph_tools.py and test_recovery.py; R10 is pinned by test_api.py,
# test_run_store.py and the web cursor/de-duplication tests.
PROCESS_MATRIX_CASES = {
    "R01": "test_r01_process_exit_after_approval_checkpoint_before_display",
    "R02": "test_r02_restart_does_not_extend_approval_expiry",
    "R03": "test_r03_to_r06_hard_crash_matrix[after_approval_checkpoint]",
    "R04": "test_r03_to_r06_hard_crash_matrix[before_commit]",
    "R05": "test_r03_to_r06_hard_crash_matrix[after_commit]",
    "R06": "test_r03_to_r06_hard_crash_matrix[before_final_answer]",
    "R07": "test_api.py approval decision concurrency and replay cases",
    "R08": "test_api.py session resume/cancel lock cases",
    "R09": "test_recovery.py fail-closed cases",
    "R10": "test_api.py and test_run_store.py durable cursor cases",
}
