"""erp_store 单测：模型约束、种子确定性与异常数据、查询 API、报价计算。

种子在 module 级 fixture 里跑一次（600 单确定性生成，秒级），各测试只读；
确定性断言单独用小规模库种两次对比。
"""

from datetime import datetime

import pytest
from erp_store import (
    Order,
    OrderItem,
    OrderStatus,
    ProductStatus,
    StockItem,
)
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import DEFAULT_SEED, seed_database
from pydantic import ValidationError

_FIXED_NOW = datetime(2026, 9, 30, 12, 0)  # 确定性断言把日期锚死

_CATEGORIES = ["茶具", "文房", "香道", "瓷器", "丝绸"]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> tuple[ErpRepository, object]:
    path = tmp_path_factory.mktemp("db") / "erp.db"
    stats = seed_database(path)
    return ErpRepository(make_engine(path)), stats


def _first_off_sale_sku(repo: ErpRepository) -> str:
    for category in _CATEGORIES:
        for p in repo.search_products(category, limit=100):
            if p.status is ProductStatus.OFF_SALE:
                return p.sku
    raise AssertionError("种子数据中没有已下架商品")


# ---- 模型约束 ----


def test_stock_quantity_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        StockItem(sku="A1001", quantity=-1)


def test_order_total_amount_derived_from_items() -> None:
    order = Order(
        order_id="SO-1",
        customer="测试",
        status=OrderStatus.PENDING_SHIPMENT,
        created_at=datetime(2026, 9, 30),
        items=[
            OrderItem(sku="A1001", name="青瓷茶具", quantity=2, unit_price=299.5),
            OrderItem(sku="B2002", name="宣纸", quantity=3, unit_price=45.4),
        ],
    )
    assert order.total_amount == round(2 * 299.5 + 3 * 45.4, 2)


# ---- 种子：确定性与规模 ----


def test_seed_is_deterministic(tmp_path) -> None:
    """同 seed + 同 now → 数据完全一致（评测可复现的前提）。"""
    a = seed_database(tmp_path / "a.db", n_products=50, n_orders=40, now=_FIXED_NOW)
    b = seed_database(tmp_path / "b.db", n_products=50, n_orders=40, now=_FIXED_NOW)

    assert a == b  # SeedStats 全字段相等


def test_seed_scale_and_time_span(seeded) -> None:
    _, stats = seeded
    assert stats.products == 300
    assert stats.orders == 600
    assert stats.first_created <= stats.last_created
    span_days = (stats.last_created - stats.first_created).days
    assert span_days >= 150  # 近 6 个月流水


def test_anomalies_are_seeded(seeded) -> None:
    """异常数据是边界 case 评测的预埋，必须存在且成规模。"""
    repo, stats = seeded
    assert stats.zero_stock >= 10  # 零库存商品
    assert stats.off_sale >= 3  # 已下架商品
    assert stats.cancelled_orders > 0 and stats.refunded_orders > 0
    assert stats.orders_with_off_sale_item > 0  # 含下架商品的订单
    assert stats.price_drift_items > 50  # 快照价 ≠ 现价

    all_statuses = {
        o.status
        for o in repo.list_orders(limit=repo.count_orders())
    }
    assert all(s in all_statuses for s in OrderStatus)  # 六种状态都出现


# ---- 查询 API ----


def test_get_order_roundtrip(seeded) -> None:
    repo, _ = seeded
    orders = repo.list_orders(limit=1)
    assert orders
    fetched = repo.get_order(orders[0].order_id)
    assert fetched == orders[0]
    assert fetched.items  # 订单明细已随单加载
    assert fetched.total_amount == round(
        sum(i.quantity * i.unit_price for i in fetched.items), 2
    )


def test_get_order_missing_returns_none(seeded) -> None:
    repo, _ = seeded
    assert repo.get_order("SO20990101-9999") is None


def test_list_orders_filters_and_paginates(seeded) -> None:
    repo, _ = seeded
    cancelled = repo.list_orders(status=OrderStatus.CANCELLED, limit=100)
    assert cancelled and all(o.status is OrderStatus.CANCELLED for o in cancelled)
    assert repo.count_orders(status=OrderStatus.CANCELLED) >= len(cancelled)

    page1 = repo.list_orders(limit=5, offset=0)
    page2 = repo.list_orders(limit=5, offset=5)
    assert len(page1) == 5 and len(page2) == 5
    assert {o.order_id for o in page1}.isdisjoint({o.order_id for o in page2})
    # 按时间倒序：第 1 页最早一条不早于第 2 页最新一条
    assert page1[-1].created_at >= page2[0].created_at


def test_search_products_by_keyword(seeded) -> None:
    repo, _ = seeded
    hits = repo.search_products("青瓷")
    assert hits and all("青瓷" in p.name or "青瓷" in p.category for p in hits)
    assert repo.search_products("不存在品类xyz") == []
    assert repo.search_products("   ") == []  # 空白关键词返回空列表


def test_get_stock(seeded) -> None:
    repo, _ = seeded
    assert repo.get_stock("NO-SUCH-SKU") is None
    some_sku = repo.search_products("茶具", limit=1)[0].sku
    stock = repo.get_stock(some_sku)
    assert stock is not None and stock.quantity >= 0


# ---- 报价计算 ----


def test_compute_quote_quantity_tiers(seeded) -> None:
    repo, _ = seeded
    sku = repo.search_products("青瓷", limit=1)[0].sku
    price = repo.get_product(sku).price

    q5 = repo.compute_quote(sku, quantity=5)
    assert q5.discount == 1.0 and q5.unit_price == round(price, 2)
    assert q5.total == round(price * 5, 2)
    for qty, disc in [(10, 0.98), (50, 0.95), (200, 0.90)]:
        quote = repo.compute_quote(sku, quantity=qty)
        assert quote.discount == disc
        assert quote.total == round(round(price * disc, 2) * qty, 2)


def test_compute_quote_flags_zero_stock(seeded) -> None:
    """零库存商品可报价（报价仅参考），库存数如实带出——工具层负责说明。"""
    repo, stats = seeded
    zero_sku = next(
        p.sku
        for p in repo.search_products("茶具", limit=100)
        if repo.get_stock(p.sku).quantity == 0
    )
    quote = repo.compute_quote(zero_sku, quantity=1)
    assert quote is not None and quote.stock_quantity == 0


def test_compute_quote_rejects_invalid(seeded) -> None:
    repo, _ = seeded
    assert repo.compute_quote("NO-SUCH-SKU", 1) is None
    sku = repo.search_products("茶具", limit=1)[0].sku
    assert repo.compute_quote(sku, 0) is None  # 数量 < 1 不报价
    assert repo.compute_quote(_first_off_sale_sku(repo), 1) is None  # 已下架不可报价


# ---- 运营视图（M3 第 3 周新增查询） ----


def test_list_products_filters_and_pagination(seeded) -> None:
    repo, _ = seeded
    off_sale = repo.list_products(status=ProductStatus.OFF_SALE, limit=100)
    assert off_sale and all(p.status is ProductStatus.OFF_SALE for p in off_sale)
    tea = repo.list_products(category="茶具", limit=100)
    assert tea and all(p.category == "茶具" for p in tea)

    total = repo.count_products(status=ProductStatus.OFF_SALE, category="茶具")
    both = repo.list_products(status=ProductStatus.OFF_SALE, category="茶具", limit=100)
    assert len(both) == total  # 组合过滤的计数与列表一致

    page1 = repo.list_products(limit=5, offset=0)
    page2 = repo.list_products(limit=5, offset=5)
    assert {p.sku for p in page1}.isdisjoint({p.sku for p in page2})


def test_get_orders_by_sku(seeded) -> None:
    repo, _ = seeded
    product = repo.search_products("茶具", limit=1)[0]
    orders = repo.get_orders_by_sku(product.sku, limit=10)
    assert repo.count_orders_by_sku(product.sku) >= len(orders)
    if orders:  # 明细成对加载，且每单确实含该 SKU
        assert all(any(i.sku == product.sku for i in o.items) for o in orders)


def test_stock_valuation_matches_manual_sum(seeded) -> None:
    repo, _ = seeded
    lines = repo.stock_valuation()
    assert lines
    line = lines[0]  # 抽验一个品类：估值 = Σ(库存数量 × 现价)
    manual = sum(
        (repo.get_stock(p.sku).quantity if repo.get_stock(p.sku) else 0) * p.price
        for p in repo.list_products(category=line.category, limit=1000)
    )
    assert line.total_value == pytest.approx(manual, abs=0.5)


def test_daily_sales_within_window(seeded) -> None:
    repo, _ = seeded
    points = repo.daily_sales(days=30)
    assert points  # 近 6 个月流水，30 天窗口必有成交
    dates = [p.date for p in points]
    assert dates == sorted(dates)
    assert (datetime.now() - datetime.fromisoformat(dates[0])).days <= 30


def test_default_seed_is_pinned() -> None:
    assert DEFAULT_SEED == 20260930  # 换种子 = 换数据集，必须显式改这里
