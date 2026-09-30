"""确定性种子数据：同 seed + 同 now 必然同数据（评测可复现的前提）。

订单日期锚定 now（默认当前时间，显式传入可完全复现）；其余一切内容
只由 seed 决定。规模与异常分布来自计划 §4：数百条商品、跨数月的订单
流水、少量异常数据。异常不是脏数据，是边界 case 评测的预埋——

- 零库存商品（有价无货，报价场景的边界）
- 已取消 / 已退款订单（状态机的非 happy path）
- 含已下架商品的订单（历史合法、现况特殊的订单）
- 下单快照价 ≠ 现价（订单查询用快照价、报价用现价的语义验证场）

确定性来自两点：random.Random(seed) 实例化（不动全局种子），且所有遍历
按列表顺序、不迭代 set/dict 视图。
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from erp_store.db import (
    Base,
    OrderItemRow,
    OrderRow,
    ProductRow,
    StockRow,
    init_db,
    make_engine,
)
from erp_store.models import OrderStatus, ProductStatus

DEFAULT_SEED = 20260930
DEFAULT_PRODUCTS = 300
DEFAULT_ORDERS = 600
DAYS_SPAN = 180  # 订单流水跨近 6 个月

# (SKU 前缀, 品类, 基础名, 规格词, 价格区间)——文创小店的人设词库
_CATALOG: list[tuple[str, str, list[str], list[str], tuple[float, float]]] = [
    ("A", "茶具", ["青瓷茶具", "紫砂壶", "公道杯", "品茗杯", "盖碗", "茶盘", "茶叶罐", "茶宠"],
     ["旅行装", "家用装", "礼盒装", "便携款"], (59.0, 899.0)),
    ("B", "文房", ["宣纸", "毛笔", "墨锭", "砚台", "镇尺", "毛毡", "笔架", "字帖"],
     ["100张", "半生半熟", "狼毫", "羊毫", "初学套", "进阶款"], (8.0, 268.0)),
    ("C", "香道", ["线香", "香炉", "香插", "香囊", "塔香"],
     ["沉香", "檀香", "艾草", "禅意款", "家用装"], (19.0, 399.0)),
    ("D", "瓷器", ["青花瓷盘", "汝窑杯", "白瓷盖碗", "青瓷花瓶", "粉彩碟"],
     ["手绘", "复古", "家用", "收藏款"], (49.0, 699.0)),
    ("E", "丝绸", ["真丝方巾", "桑蚕丝枕套", "真丝眼罩", "刺绣团扇"],
     ["素色", "印花", "苏绣", "双面"], (69.0, 599.0)),
]

_CUSTOMERS = [
    "沈砚秋", "顾青山", "林晚照", "苏辞", "陆修远", "白鹿", "江疏影阁", "程素心",
    "闻人语", "周砚白", "许清欢", "叶知秋", "秦观澜", "方聿修", "崔九思",
    "温故", "陶然", "薛小满", "韩青梧", "齐白石斋",
]

_NOTES = [
    "客户要求尽快发货",
    "礼品订单，需要重新包装",
    "地址偏远，走邮政",
    "老客户，优先处理",
    "发票抬头见备注邮件",
]


@dataclass(frozen=True, slots=True)
class SeedStats:
    products: int
    stocks: int
    orders: int
    order_items: int
    zero_stock: int
    off_sale: int
    cancelled_orders: int
    refunded_orders: int
    orders_with_off_sale_item: int
    price_drift_items: int
    first_created: datetime
    last_created: datetime


def generate_products(rng: random.Random, count: int) -> list[ProductRow]:
    rows: list[ProductRow] = []
    n = 0
    while len(rows) < count:
        for code, category, bases, specs, (lo, hi) in _CATALOG:
            for base in bases:
                for spec in specs:
                    if len(rows) >= count:
                        return rows
                    n += 1
                    rows.append(ProductRow(
                        sku=f"{code}{1000 + n}",
                        name=f"{base}（{spec}）",
                        category=category,
                        price=round(rng.uniform(lo, hi), 1),
                        status=(
                            ProductStatus.OFF_SALE.value
                            if rng.random() < 0.03
                            else ProductStatus.ON_SALE.value
                        ),
                    ))
    return rows


def generate_stocks(rng: random.Random, skus: list[str]) -> list[StockRow]:
    """约 8% 零库存（有价无货），其余 5~200 件。"""
    return [
        StockRow(
            sku=sku,
            quantity=0 if rng.random() < 0.08 else rng.randint(5, 200),
        )
        for sku in skus
    ]


def generate_orders(
    rng: random.Random,
    products: list[ProductRow],
    count: int,
    *,
    now: datetime | None = None,
) -> tuple[list[OrderRow], int]:
    """近 6 个月订单流水，返回 (订单行, 明细行数)。

    状态按单据年龄分布（老的已签收为主、新的多在待付款/待发货）；约 10% 的
    明细用历史快照价（≠ 现价）；约 5% 的订单强制含一件已下架商品。
    """
    now = now or datetime.now()
    price_of = {p.sku: p.price for p in products}
    name_of = {p.sku: p.name for p in products}
    on_sale = [p for p in products if p.status == ProductStatus.ON_SALE.value]
    off_sale = [p for p in products if p.status == ProductStatus.OFF_SALE.value]

    rows: list[OrderRow] = []
    item_count = 0
    for i in range(count):
        age_days = rng.uniform(0, DAYS_SPAN)
        created = now - timedelta(days=age_days, minutes=rng.uniform(0, 60 * 24))
        items: list[OrderItemRow] = []
        k = min(rng.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0],
                len(on_sale))
        for sku in rng.sample([p.sku for p in on_sale], k=k):
            quantity = rng.choices(
                [1, 2, 5, 10, 20], weights=[45, 25, 15, 10, 5])[0]
            snapshot = price_of[sku]
            if rng.random() < 0.10:  # 历史调价：下单快照价偏离现价
                snapshot = round(snapshot * rng.uniform(0.88, 1.12), 2)
            items.append(OrderItemRow(
                sku=sku, name=name_of[sku], quantity=quantity,
                unit_price=round(snapshot, 2),
            ))
        if off_sale and rng.random() < 0.05:  # 历史合法、现况特殊：含已下架商品
            p = rng.choice(off_sale)
            items.append(OrderItemRow(
                sku=p.sku, name=p.name,
                quantity=rng.randint(1, 3),
                unit_price=round(p.price, 2),
            ))
        item_count += len(items)
        rows.append(OrderRow(
            order_id=f"SO{created:%Y%m%d}-{i + 1:04d}",
            customer=rng.choice(_CUSTOMERS),
            status=_status_for_age(rng, age_days).value,
            created_at=created,
            note=rng.choice(_NOTES) if rng.random() < 0.05 else None,
            items=items,
        ))
    rows.sort(key=lambda r: r.created_at)
    return rows, item_count


def _status_for_age(rng: random.Random, age_days: float) -> OrderStatus:
    if age_days > 30:
        return rng.choices(
            [OrderStatus.DELIVERED, OrderStatus.SHIPPED, OrderStatus.CANCELLED,
             OrderStatus.REFUNDED],
            weights=[70, 12, 12, 6])[0]
    if age_days > 3:
        return rng.choices(
            [OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.PENDING_SHIPMENT,
             OrderStatus.CANCELLED, OrderStatus.REFUNDED],
            weights=[45, 30, 15, 5, 5])[0]
    return rng.choices(
        [OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING_SHIPMENT, OrderStatus.SHIPPED],
        weights=[35, 45, 20])[0]


def seed_database(
    db_path: Path,
    *,
    seed: int = DEFAULT_SEED,
    n_products: int = DEFAULT_PRODUCTS,
    n_orders: int = DEFAULT_ORDERS,
    now: datetime | None = None,
) -> SeedStats:
    """全量重建数据库（drop + create），返回统计供 CLI 与测试断言。"""
    rng = random.Random(seed)
    engine = make_engine(db_path)
    Base.metadata.drop_all(engine)
    init_db(engine)

    products = generate_products(rng, n_products)
    stocks = generate_stocks(rng, [p.sku for p in products])
    orders, item_count = generate_orders(rng, products, n_orders, now=now)
    prices = price_map(products)
    off_sale_skus = {p.sku for p in products if p.status == ProductStatus.OFF_SALE.value}

    with Session(engine) as session:
        session.add_all(products)
        session.add_all(stocks)
        session.add_all(orders)
        # 统计必须在 commit 前算完：commit 会过期对象，之后访问属性要重查库
        stats = SeedStats(
            products=len(products),
            stocks=len(stocks),
            orders=len(orders),
            order_items=item_count,
            zero_stock=sum(s.quantity == 0 for s in stocks),
            off_sale=sum(p.status == ProductStatus.OFF_SALE.value for p in products),
            cancelled_orders=sum(
                o.status == OrderStatus.CANCELLED.value for o in orders
            ),
            refunded_orders=sum(
                o.status == OrderStatus.REFUNDED.value for o in orders
            ),
            orders_with_off_sale_item=sum(
                any(i.sku in off_sale_skus for i in o.items) for o in orders
            ),
            price_drift_items=sum(
                1 for o in orders for i in o.items if i.unit_price != prices[i.sku]
            ),
            first_created=min(o.created_at for o in orders),
            last_created=max(o.created_at for o in orders),
        )
        session.commit()

    return stats


def price_map(products: list[ProductRow]) -> dict[str, float]:
    return {p.sku: p.price for p in products}
