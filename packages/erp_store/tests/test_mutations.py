"""erp_store 写 API 单测（M4 第 1 周，ADR-0005）：领域规则与错误契约。

写测试用独立的小库（function 级 fixture，写操作会改数据，模块级共享会互相
污染）；只读侧的对照查询用同一 engine 的 ErpRepository。
"""

import pytest
from erp_store import (
    CANCELLABLE_STATUSES,
    ErpMutations,
    ErpRepository,
    MutationError,
    OrderStatus,
    ProductStatus,
)
from erp_store.db import make_engine
from erp_store.seed import seed_database


@pytest.fixture()
def store(tmp_path) -> tuple[ErpRepository, ErpMutations]:
    """每测试一个独立小库：读写两个入口共用同一 engine。"""
    path = tmp_path / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    engine = make_engine(path)
    return ErpRepository(engine), ErpMutations(engine)


def _on_sale_with_stock(repo: ErpRepository, need: int = 1) -> tuple[str, int]:
    """找一件在售且库存充足的商品，返回 (sku, 库存)。"""
    for p in repo.list_products(status=ProductStatus.ON_SALE, limit=50):
        stock = repo.get_stock(p.sku)
        if stock and stock.quantity >= need:
            return p.sku, stock.quantity
    raise AssertionError("种子库中没有在售且有库存的商品")


def test_create_order_snapshots_price_and_deducts_stock(store) -> None:
    repo, mutations = store
    sku, stock_before = _on_sale_with_stock(repo, need=2)
    price = repo.get_product(sku).price

    order = mutations.create_order("测试客户", [(sku, 2)])

    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order.customer == "测试客户"
    assert order.items[0].sku == sku
    assert order.items[0].unit_price == price  # 快照价 = 下单时现价
    assert order.total_amount == pytest.approx(round(price * 2, 2))
    assert repo.get_stock(sku).quantity == stock_before - 2  # 同事务扣减
    assert repo.get_order(order.order_id) is not None  # 单号格式与种子一致可反查


def test_create_order_merges_same_sku_and_generates_sequence(store) -> None:
    repo, mutations = store
    sku, _ = _on_sale_with_stock(repo, need=3)

    order = mutations.create_order("测试客户", [(sku, 1), (sku, 2)])

    assert len(order.items) == 1 and order.items[0].quantity == 3
    assert order.order_id.startswith("SO")  # SO+日期+序号，与查询侧格式约定一致


def test_create_order_rejects_insufficient_stock(store) -> None:
    repo, mutations = store
    sku, stock = _on_sale_with_stock(repo)

    with pytest.raises(MutationError) as exc:
        mutations.create_order("测试客户", [(sku, stock + 1)])
    assert exc.value.code == "insufficient_stock"
    assert str(stock) in exc.value.message
    assert repo.get_stock(sku).quantity == stock  # 失败不留半截：库存未动


def test_create_order_rejects_off_sale_and_missing_sku(store) -> None:
    repo, mutations = store
    off_sale = next(
        p.sku for p in repo.list_products(status=ProductStatus.OFF_SALE, limit=1)
    )

    with pytest.raises(MutationError) as exc:
        mutations.create_order("测试客户", [(off_sale, 1)])
    assert exc.value.code == "invalid_transition"

    with pytest.raises(MutationError) as exc:
        mutations.create_order("测试客户", [("ZZZ999", 1)])
    assert exc.value.code == "not_found"


def test_create_order_rejects_empty_customer_and_items(store) -> None:
    _repo, mutations = store
    with pytest.raises(MutationError) as exc:
        mutations.create_order("  ", [("A1001", 1)])
    assert exc.value.code == "invalid_argument"
    with pytest.raises(MutationError):
        mutations.create_order("测试客户", [])


def test_cancel_order_restores_stock_and_enforces_state_machine(store) -> None:
    repo, mutations = store
    sku, stock_before = _on_sale_with_stock(repo, need=2)
    order = mutations.create_order("测试客户", [(sku, 2)])

    cancelled = mutations.cancel_order(order.order_id)

    assert cancelled.status is OrderStatus.CANCELLED
    assert repo.get_stock(sku).quantity == stock_before  # 回补后与下单前一致

    # 已取消的订单再取消 → invalid_transition（状态机拒绝重复迁移）
    with pytest.raises(MutationError) as exc:
        mutations.cancel_order(order.order_id)
    assert exc.value.code == "invalid_transition"


def test_cancel_order_rejects_non_cancellable_states(store) -> None:
    """种子里的已签收/已取消单都不可取消；仅待付款/待发货在白名单。"""
    repo, mutations = store
    delivered = repo.list_orders(status=OrderStatus.DELIVERED, limit=1)[0]
    with pytest.raises(MutationError) as exc:
        mutations.cancel_order(delivered.order_id)
    assert exc.value.code == "invalid_transition"
    assert delivered.status.value in ("已签收",)

    assert set(CANCELLABLE_STATUSES) == {
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.PENDING_SHIPMENT,
    }


def test_cancel_order_missing(store) -> None:
    _repo, mutations = store
    with pytest.raises(MutationError) as exc:
        mutations.cancel_order("SO20990101-9999")
    assert exc.value.code == "not_found"


def test_adjust_stock_delta_semantics(store) -> None:
    repo, mutations = store
    sku, stock = _on_sale_with_stock(repo)

    _, after_in = mutations.adjust_stock(sku, 5)
    assert after_in == stock + 5

    _, after_out = mutations.adjust_stock(sku, -2)
    assert after_out == stock + 3
    assert repo.get_stock(sku).quantity == stock + 3

    with pytest.raises(MutationError) as exc:
        mutations.adjust_stock(sku, -(stock + 100))
    assert exc.value.code == "insufficient_stock"
    with pytest.raises(MutationError):
        mutations.adjust_stock(sku, 0)


def test_set_product_status_reports_no_op_as_transition_error(store) -> None:
    """空操作报 invalid_transition 而非成功——返回值会被模型当作战果转述。"""
    repo, mutations = store
    sku = repo.list_products(status=ProductStatus.ON_SALE, limit=1)[0].sku

    assert mutations.set_product_status(sku, ProductStatus.OFF_SALE) == sku
    assert repo.get_product(sku).status is ProductStatus.OFF_SALE

    with pytest.raises(MutationError) as exc:
        mutations.set_product_status(sku, ProductStatus.OFF_SALE)
    assert exc.value.code == "invalid_transition"
    assert "无需变更" in exc.value.message


def test_lookup_result_returns_original_result_for_each_mutation(store) -> None:
    repo, mutations = store
    sku, _stock = _on_sale_with_stock(repo, need=2)

    order_args = {
        "customer": "恢复核对客户",
        "items": [{"sku": sku, "quantity": 1}],
        "note": None,
    }
    order = mutations.create_order(
        "恢复核对客户", [(sku, 1)], client_token="token-create"
    )
    cancel_args = {"order_id": order.order_id}
    cancelled = mutations.cancel_order(order.order_id, client_token="token-cancel")
    stock_args = {"sku": sku, "delta": 4}
    stock_result = mutations.adjust_stock(sku, 4, client_token="token-stock")
    status_args = {"sku": sku, "status": ProductStatus.OFF_SALE.value}
    status_result = mutations.set_product_status(
        sku, ProductStatus.OFF_SALE, client_token="token-status"
    )

    lookups = [
        ("create_order", order_args, "token-create", order.model_dump(mode="json")),
        (
            "cancel_order",
            cancel_args,
            "token-cancel",
            cancelled.model_dump(mode="json"),
        ),
        ("adjust_stock", stock_args, "token-stock", list(stock_result)),
        ("set_product_status", status_args, "token-status", status_result),
    ]
    before_stock = repo.get_stock(sku).quantity
    before_status = repo.get_product(sku).status
    before_order = repo.get_order(order.order_id)

    for tool_name, arguments, token, expected in lookups:
        status, result = mutations.lookup_result(tool_name, arguments, token)
        assert status == "found"
        assert result == expected

    assert repo.get_stock(sku).quantity == before_stock
    assert repo.get_product(sku).status is before_status
    assert repo.get_order(order.order_id) == before_order


def test_lookup_result_distinguishes_absent_token_from_argument_conflict(store) -> None:
    repo, mutations = store
    sku, stock_before = _on_sale_with_stock(repo)
    mutations.adjust_stock(sku, 3, client_token="token-bound-to-plus-three")
    after_write = repo.get_stock(sku).quantity

    assert mutations.lookup_result("adjust_stock", {"sku": sku, "delta": 9},
                                   "token-never-seen") == ("absent", None)
    assert mutations.lookup_result("adjust_stock", {"sku": sku, "delta": 9},
                                   "token-bound-to-plus-three") == ("conflict", None)
    assert repo.get_stock(sku).quantity == after_write == stock_before + 3
