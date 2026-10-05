"""占位符 → 种子库真实值：标注与数据解耦（标注标准 §4）。

种子库是确定性的，但 case 里不写死单号/SKU——写 {placeholder}，runner 启动时
从种子库解析。新增占位符必须同步本模块与 test_cases 的静态校验。
"""

from erp_store.models import OrderStatus, ProductStatus
from erp_store.repository import ErpRepository


def resolve(repo: ErpRepository) -> dict[str, str]:
    """从种子库解析全部占位符；缺某类异常数据时 KeyError 直接暴露（不静默）。"""
    resolved: dict[str, str] = {}

    def order(status: OrderStatus, key: str) -> None:
        rows = repo.list_orders(status=status, limit=1)
        assert rows, f"种子库缺少 {status.value} 订单，占位符 {{{key}}} 无法解析"
        resolved[key] = rows[0].order_id

    order(OrderStatus.PENDING_SHIPMENT, "order_id")
    order(OrderStatus.CANCELLED, "cancelled_order_id")
    order(OrderStatus.DELIVERED, "delivered_order_id")

    def product(status: ProductStatus, prefix: str) -> None:
        rows = repo.list_products(status=status, limit=1)
        assert rows, f"种子库缺少 {status.value} 商品，占位符 {{{prefix}_*}} 无法解析"
        resolved[f"{prefix}_sku"] = rows[0].sku
        resolved[f"{prefix}_name"] = rows[0].name

    product(ProductStatus.OFF_SALE, "off_sale")
    product(ProductStatus.ON_SALE, "on_sale")

    zero = repo.list_low_stock(threshold=0, limit=1)
    assert zero, "种子库缺少零库存商品，占位符 {zero_stock_*} 无法解析"
    resolved["zero_stock_sku"] = zero[0].sku
    resolved["zero_stock_name"] = zero[0].name

    top = repo.top_products(days=30, limit=1)
    assert top, "种子库缺少近 30 天销量数据，占位符 {top_*} 无法解析"
    resolved["top_sku"] = top[0].sku
    resolved["top_name"] = top[0].name

    # 客户：取近期订单里出现最多的客户名（get_customer_purchases 场景）
    recent = repo.list_orders(limit=50)
    counts: dict[str, int] = {}
    for o in recent:
        counts[o.customer] = counts.get(o.customer, 0) + 1
    resolved["customer"] = max(counts, key=lambda c: counts[c])

    # 快照价 ≠ 现价的订单：翻页扫全量订单明细找第一笔（种子预埋的异常数据）
    resolved.update(_snapshot_order(repo))
    return resolved


def _snapshot_order(repo: ErpRepository) -> dict[str, str]:
    """找一笔明细里"下单快照价与现价不一致"的订单（在售商品，语义才成立）。"""
    offset = 0
    while True:
        page = repo.list_orders(limit=50, offset=offset)
        if not page:
            break
        for o in page:
            for item in o.items:
                p = repo.get_product(item.sku)
                if p is not None and p.status is ProductStatus.ON_SALE \
                        and p.price != item.unit_price:
                    return {
                        "snapshot_order_id": o.order_id,
                        "snapshot_sku": item.sku,
                        "snapshot_name": item.name,
                    }
        offset += 50
    raise AssertionError("种子库缺少快照价≠现价的订单（预埋异常数据缺失）")
