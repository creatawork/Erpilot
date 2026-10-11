from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from erp_store.db import OrderRow, ProductRow, StockRow, init_db, make_engine
from erp_store.models import OrderStatus, ProductStatus
from erp_store.mutations import MutationError
from erp_store.operations import ErpOperations
from erp_store.repository import ErpRepository
from sqlalchemy.orm import Session


@pytest.fixture
def store(tmp_path):
    engine = make_engine(tmp_path / "erp.db")
    init_db(engine)
    with Session(engine) as s:
        s.add(
            ProductRow(
                sku="A", name="商品", category="测试", price=100, status=ProductStatus.ON_SALE.value
            )
        )
        s.add(StockRow(sku="A", quantity=100, warehouse="主仓"))
        s.commit()
    ops = ErpOperations(engine)
    return engine, ErpRepository(engine), ops


def test_preview_adjust_has_exact_impact_without_changing_stock(store):
    _, repo, ops = store
    preview = ops.prepare_adjust("s1", "A", -3)
    assert preview["summary"] == dict(
        sku="A",
        name="商品",
        delta=-3,
        quantity_before=100,
        quantity_after=97,
        warehouse="主仓",
        stock_exists=True,
    )
    assert preview["status"] == "open"
    assert repo.get_stock("A").quantity == 100


def test_cancel_preview_and_execution_use_original_order_prices(store):
    _, repo, ops = store
    order = ops.create_order("客户", [("A", 10)])
    preview = ops.prepare_cancel("s1", order.order_id)
    assert preview["summary"]["customer"] == "客户"
    assert preview["summary"]["total_amount"] == 980
    assert preview["summary"]["items"][0] == dict(
        sku="A",
        name="商品",
        quantity=10,
        unit_price=98,
        line_total=980,
        stock_before=90,
        stock_after=100,
        warehouse="主仓",
    )
    assert repo.get_order(order.order_id).status == OrderStatus.PENDING_PAYMENT
    assert repo.get_stock("A").quantity == 90
    result = ops.execute("s1", preview["operation_id"], "cancel_order", "approved")
    assert result["status"] == OrderStatus.CANCELLED.value
    assert repo.get_stock("A").quantity == 100
    assert ops.get("s1", preview["operation_id"])["status"] == "executed"


@pytest.mark.parametrize("field,value", [("quantity", 101), ("warehouse", "新仓")])
def test_stock_changes_require_new_preview(store, field, value):
    engine, repo, ops = store
    preview = ops.prepare_adjust("s1", "A", 5)
    with Session(engine) as s:
        setattr(s.get(StockRow, "A"), field, value)
        s.commit()
    assert ops.get("s1", preview["operation_id"])["status"] == "changed"
    with pytest.raises(MutationError) as err:
        ops.execute("s1", preview["operation_id"], "adjust_stock", "approved")
    assert err.value.code == "operation_changed"
    assert repo.get_stock("A").quantity == (101 if field == "quantity" else 100)


@pytest.mark.parametrize("change", ["status", "items", "stock"])
def test_cancellation_changes_invalidate_even_if_still_cancellable(store, change):
    engine, repo, ops = store
    order = ops.create_order("客户", [("A", 10)])
    preview = ops.prepare_cancel("s1", order.order_id)
    with Session(engine) as s:
        row = s.get(OrderRow, order.order_id)
        if change == "status":
            row.status = OrderStatus.PENDING_SHIPMENT.value
        elif change == "items":
            row.items[0].quantity = 9
        else:
            s.get(StockRow, "A").quantity = 91
        s.commit()
    with pytest.raises(MutationError) as err:
        ops.execute("s1", preview["operation_id"], "cancel_order", "approved")
    assert err.value.code == "operation_changed"
    assert repo.get_order(order.order_id).status != OrderStatus.CANCELLED


def test_missing_stock_is_explicit_for_cancel_and_can_be_created_for_adjust(store):
    engine, repo, ops = store
    order = ops.create_order("客户", [("A", 10)])
    with Session(engine) as s:
        s.delete(s.get(StockRow, "A"))
        s.commit()
    cancel = ops.prepare_cancel("s1", order.order_id)
    assert cancel["summary"]["items"][0]["stock_after"] is None
    ops.execute("s1", cancel["operation_id"], "cancel_order", "cancel")
    assert repo.get_stock("A") is None
    adjust = ops.prepare_adjust("s1", "A", 3)
    assert adjust["summary"]["stock_exists"] is False
    result = ops.execute("s1", adjust["operation_id"], "adjust_stock", "adjust")
    assert result["quantity"] == 3 and repo.get_stock("A").quantity == 3


def test_expiry_boundary_and_replay_after_expiry(store):
    engine, repo, _ = store
    now = datetime(2026, 10, 11, tzinfo=UTC)
    ops = ErpOperations(engine, clock=lambda: now)
    expired = ops.prepare_adjust("s1", "A", 5)
    done = ops.prepare_adjust("s1", "A", 5)
    result = ops.execute("s1", done["operation_id"], "adjust_stock", "first")
    now += timedelta(minutes=30)
    assert ops.get("s1", expired["operation_id"])["status"] == "expired"
    with pytest.raises(MutationError) as err:
        ops.execute("s1", expired["operation_id"], "adjust_stock", "expired")
    assert err.value.code == "operation_expired"
    assert ops.execute("s1", done["operation_id"], "adjust_stock", "new") == result
    assert repo.get_stock("A").quantity == 105


def test_foreign_owner_kind_and_token_cannot_reveal_or_execute(store):
    _, repo, ops = store
    p = ops.prepare_adjust("s1", "A", 5)
    for args in [("s2", "adjust_stock"), ("s1", "cancel_order")]:
        with pytest.raises(MutationError) as err:
            ops.execute(args[0], p["operation_id"], args[1], "token")
        assert err.value.code == "not_found"
    result = ops.execute("s1", p["operation_id"], "adjust_stock", "token")
    assert result["quantity"] == 105
    args = {"operation_id": p["operation_id"]}
    assert ops.lookup_operation("s2", "adjust_stock", args, "token") == ("absent", None)
    assert ops.lookup_operation("s1", "cancel_order", args, "token") == ("absent", None)
    assert ops.lookup_operation("s1", "adjust_stock", args, "token") == ("found", result)
    with pytest.raises(MutationError):
        ops.get("s2", p["operation_id"])
    assert repo.get_stock("A").quantity == 105


def test_parallel_new_tokens_apply_impact_once(store):
    engine, repo, ops = store
    p = ops.prepare_adjust("s1", "A", 5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda i: ErpOperations(engine).execute(
                    "s1", p["operation_id"], "adjust_stock", f"token-{i}"
                ),
                range(4),
            )
        )
    assert all(r["quantity"] == 105 for r in results)
    assert repo.get_stock("A").quantity == 105
    p2 = ops.prepare_adjust("s1", "A", 3)
    with pytest.raises(MutationError) as err:
        ops.execute("s1", p2["operation_id"], "adjust_stock", "token-0")
    assert err.value.code == "idempotency_conflict"


@pytest.mark.parametrize("delta", [0, True, 1.5, -101])
def test_invalid_adjust_is_rejected_before_approval(store, delta):
    _, repo, ops = store
    with pytest.raises(MutationError):
        ops.prepare_adjust("s1", "A", delta)
    assert repo.get_stock("A").quantity == 100
