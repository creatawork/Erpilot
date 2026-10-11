"""Fixed-price business regressions: quotes, orders, snapshots and rollback."""

import json
from datetime import datetime

import pytest
from erp_store import ErpMutations, ErpRepository, MutationError, ProductStatus
from erp_store.db import (
    MutationRequestRow,
    OrderItemRow,
    OrderRow,
    ProductRow,
    StockRow,
    init_db,
    make_engine,
)
from sqlalchemy.orm import Session


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "pricing.db"
    engine = make_engine(path)
    init_db(engine)
    with Session(engine) as session:
        for sku, price in [("FIXED", 100.0), ("HALF", 0.75), ("OTHER", 20.0)]:
            session.add(ProductRow(
                sku=sku, name=sku, category="测试", price=price,
                status=ProductStatus.ON_SALE.value,
            ))
            session.add(StockRow(sku=sku, quantity=1000, warehouse="主仓"))
        session.commit()
    return path, engine, ErpRepository(engine), ErpMutations(engine)


@pytest.mark.parametrize("quantity,unit,total", [
    (1, 100.0, 100.0),
    (9, 100.0, 900.0),
    (10, 98.0, 980.0),
    (49, 98.0, 4802.0),
    (50, 95.0, 4750.0),
    (199, 95.0, 18905.0),
    (200, 90.0, 18000.0),
    (201, 90.0, 18090.0),
])
def test_quote_and_order_apply_same_per_sku_discount(store, quantity, unit, total):
    _, _, repo, mutations = store
    quote = repo.compute_quote("FIXED", quantity)
    order = mutations.create_order("折扣客户", [("FIXED", quantity)])
    assert quote.unit_price == unit
    assert quote.total == total
    assert order.items[0].unit_price == unit
    assert order.total_amount == total
    assert repo.get_order(order.order_id).total_amount == total
    assert repo.get_stock("FIXED").quantity == 1000 - quantity


def test_quote_and_order_round_half_cent_up_before_multiplying(store):
    _, _, repo, mutations = store
    quote = repo.compute_quote("HALF", 10)
    order = mutations.create_order("半分客户", [("HALF", 10)])
    # 0.75 * 0.98 = 0.735 -> 0.74; ten rounded unit prices total 7.40.
    assert quote.unit_price == 0.74
    assert quote.total == 7.4
    assert order.items[0].unit_price == 0.74
    assert order.total_amount == 7.4


def test_same_sku_lines_merge_before_discount_and_other_sku_does_not(store):
    _, _, repo, mutations = store
    order = mutations.create_order(
        "合并客户", [("FIXED", 6), ("OTHER", 5), ("FIXED", 4)]
    )
    lines = {line.sku: line for line in order.items}
    assert len(lines) == 2
    assert lines["FIXED"].quantity == 10
    assert lines["FIXED"].unit_price == 98.0
    assert lines["OTHER"].unit_price == 20.0
    assert order.total_amount == 1080.0
    assert repo.get_stock("FIXED").quantity == 990
    assert repo.get_stock("OTHER").quantity == 995


def test_discount_never_accumulates_across_skus(store):
    _, _, _, mutations = store
    order = mutations.create_order("独立档位", [("FIXED", 5), ("OTHER", 5)])
    assert {line.sku: line.unit_price for line in order.items} == {
        "FIXED": 100.0, "OTHER": 20.0,
    }
    assert order.total_amount == 600.0


def test_restart_replay_keeps_original_discounted_snapshot_after_price_change(store):
    path, engine, repo, mutations = store
    first = mutations.create_order("幂等客户", [("FIXED", 10)], client_token="priced-order")
    assert first.items[0].unit_price == 98.0
    assert first.total_amount == 980.0
    with Session(engine) as session:
        session.get(ProductRow, "FIXED").price = 200.0
        session.commit()
    assert repo.compute_quote("FIXED", 10).total == 1960.0
    restarted = ErpMutations(make_engine(path))
    replay = restarted.create_order(
        "幂等客户", [("FIXED", 6), ("FIXED", 4)], client_token="priced-order",
    )
    assert replay == first
    assert repo.get_order(first.order_id).total_amount == 980.0
    assert repo.get_stock("FIXED").quantity == 990
    assert repo.count_orders(customer="幂等客户") == 1
    state, result = restarted.lookup_result(
        "create_order",
        {"customer": "幂等客户", "items": [{"sku": "FIXED", "quantity": 10}]},
        "priced-order",
    )
    assert state == "found" and result["total_amount"] == 980.0


def test_discounted_order_cancel_restores_stock(store):
    _, _, repo, mutations = store
    order = mutations.create_order("取消折扣单", [("FIXED", 50)])
    assert order.total_amount == 4750.0
    cancelled = mutations.cancel_order(order.order_id)
    assert cancelled.total_amount == 4750.0
    assert repo.get_stock("FIXED").quantity == 1000


def test_multiline_insufficient_stock_has_no_partial_commit(store):
    _, _, repo, mutations = store
    with pytest.raises(MutationError) as error:
        mutations.create_order(
            "失败客户", [("FIXED", 50), ("OTHER", 1001)], client_token="failed-order",
        )
    assert error.value.code == "insufficient_stock"
    assert repo.count_orders(customer="失败客户") == 0
    assert repo.get_stock("FIXED").quantity == 1000
    assert repo.get_stock("OTHER").quantity == 1000
    # A failed request must not consume its token; replenishing enables the original request.
    mutations.adjust_stock("OTHER", 1)
    order = mutations.create_order(
        "失败客户", [("FIXED", 50), ("OTHER", 1001)], client_token="failed-order",
    )
    assert order.total_amount == 22768.0  # 95 * 50 + 18 * 1001.


def test_multiline_order_total_uses_rounded_settlement_units(store):
    _, engine, _, mutations = store
    with Session(engine) as session:
        session.get(ProductRow, "FIXED").price = 1.0
        session.get(ProductRow, "OTHER").price = 1.01
        session.get(ProductRow, "HALF").price = 0.665
        session.commit()
    order = mutations.create_order("金额客户", [("FIXED", 1), ("OTHER", 1), ("HALF", 1)])
    assert {line.sku: line.unit_price for line in order.items} == {
        "FIXED": 1.0, "OTHER": 1.01, "HALF": 0.67,
    }
    assert order.total_amount == 2.68


def test_legacy_subcent_snapshot_and_token_keep_original_total(store):
    _, engine, repo, mutations = store
    prior = {
        "order_id": "SO20260901-0001", "customer": "旧客户", "status": "待付款",
        "created_at": "2026-09-01T12:00:00", "note": None,
        "items": [{"sku": "FIXED", "name": "FIXED", "quantity": 1, "unit_price": 1.005}],
        "total_amount": 1.0,
    }
    with Session(engine) as session:
        session.add(OrderRow(
            order_id=prior["order_id"], customer="旧客户", status="待付款",
            created_at=datetime(2026, 9, 1, 12),
            items=[OrderItemRow(sku="FIXED", name="FIXED", quantity=1, unit_price=1.005)],
        ))
        session.add(MutationRequestRow(
            client_token="legacy-order",
            request=json.dumps(["create_order", "旧客户", [["FIXED", 1]], None],
                               ensure_ascii=False, sort_keys=True),
            result=json.dumps(prior, ensure_ascii=False),
        ))
        session.commit()
    assert repo.get_order(prior["order_id"]).total_amount == 1.0
    replay = mutations.create_order("旧客户", [("FIXED", 1)], client_token="legacy-order")
    assert replay.model_dump(mode="json") == prior
    assert repo.get_stock("FIXED").quantity == 1000


def test_zero_settlement_price_rejected_without_order_or_stock_change(store):
    _, engine, repo, mutations = store
    with Session(engine) as session:
        session.get(ProductRow, "HALF").price = 0.005
        session.commit()
    with pytest.raises(MutationError) as error:
        mutations.create_order("零成交价", [("HALF", 200)], client_token="zero-price")
    assert error.value.code == "invalid_argument"
    assert repo.get_stock("HALF").quantity == 1000
    assert repo.count_orders(customer="零成交价") == 0
    # Rejecting invalid settlement must not reserve the idempotency token.
    with Session(engine) as session:
        session.get(ProductRow, "HALF").price = 0.01
        session.commit()
    created = mutations.create_order("零成交价", [("HALF", 200)], client_token="zero-price")
    assert created.total_amount == 2.0
