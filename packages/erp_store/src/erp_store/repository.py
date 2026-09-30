"""只读查询 API：M3 工具层（mcp_erp）取数的唯一入口。

写操作刻意不在此层——动账动货要请示东家（HITL，M4+），接口先不给。
所有查询返回 models.py 的 Pydantic 模型，工具层拿到的直接是可序列化对象。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from erp_store.db import OrderItemRow, OrderRow, ProductRow, StockRow
from erp_store.models import (
    CategoryStat,
    CustomerPurchases,
    DailySalesPoint,
    LowStockItem,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductSales,
    ProductStatus,
    PurchaseLine,
    Quote,
    SalesSummary,
    StatusAmount,
    StockItem,
    StockValuationLine,
)

# 数量门槛 → 折扣，从高到低取第一个命中的档
QUOTE_TIERS: list[tuple[int, float]] = [(200, 0.90), (50, 0.95), (10, 0.98)]

# 销量统计的有效口径：已成交未流失的订单（取消 / 退款 / 待付款不计入）
VALID_SALES_STATUSES = (
    OrderStatus.PENDING_SHIPMENT,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)


class ErpRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self._engine) as session:
            yield session

    # ---- 商品 / 库存 ----

    def get_product(self, sku: str) -> Product | None:
        with self._session() as s:
            row = s.get(ProductRow, sku)
            return _product(row) if row else None

    def search_products(self, keyword: str, *, limit: int = 20) -> list[Product]:
        """按名称/品类模糊搜索；keyword 两端去空格，空串返回空列表。"""
        kw = keyword.strip()
        if not kw:
            return []
        stmt = _product_query(keyword=kw).limit(limit)
        with self._session() as s:
            return [_product(r) for r in s.scalars(stmt)]

    def count_products(
        self,
        keyword: str | None = None,
        *,
        status: ProductStatus | None = None,
        category: str | None = None,
    ) -> int:
        """与 list_products 同口径的计数（供分页判断）。"""
        stmt = select(func.count()).select_from(
            _product_query(keyword=keyword, status=status, category=category).subquery()
        )
        with self._session() as s:
            return s.scalar(stmt) or 0

    def list_products(
        self,
        keyword: str | None = None,
        *,
        status: ProductStatus | None = None,
        category: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Product]:
        """商品列表：可按关键词/状态/品类组合过滤，按 SKU 排序。"""
        stmt = (
            _product_query(keyword=keyword, status=status, category=category)
            .limit(limit)
            .offset(offset)
        )
        with self._session() as s:
            return [_product(r) for r in s.scalars(stmt)]

    def get_orders_by_sku(self, sku: str, *, limit: int = 20) -> list[Order]:
        """反查：某 SKU 出现在哪些订单里（含全部状态），按下单时间倒序。"""
        stmt = (
            select(OrderRow)
            .join(OrderItemRow, OrderItemRow.order_id == OrderRow.order_id)
            .where(OrderItemRow.sku == sku)
            .order_by(OrderRow.created_at.desc(), OrderRow.order_id)
            .limit(limit)
        )
        with self._session() as s:
            return [
                _order(r)
                for r in s.scalars(stmt.options(selectinload(OrderRow.items)))
            ]

    def count_orders_by_sku(self, sku: str) -> int:
        stmt = (
            select(func.count(func.distinct(OrderItemRow.order_id)))
            .where(OrderItemRow.sku == sku)
        )
        with self._session() as s:
            return s.scalar(stmt) or 0

    def customer_purchases(self, customer: str) -> CustomerPurchases | None:
        """聚合某客户的全部订单：买过什么（按 SKU 聚合件数/金额）、各状态
        多少单多少钱、总花费（有效口径 + 全口径）。

        这是"某人买了些什么/总共多少钱"类问题的正解——一次聚合返回紧凑结果，
        代替把该客户全部订单明细怼进上下文（那会把下一轮 LLM 请求撑爆，
        见 docs/error-recovery-log.md #2）。先计数再按数取全量，不做固定上限
        截断——截断会静默少算。客户无订单返回 None。
        """
        total = self.count_orders(customer=customer)
        if not total:
            return None
        orders = self.list_orders(customer=customer, limit=total)
        by_status: dict[str, tuple[int, float]] = {}
        items: dict[tuple[str, str], tuple[int, float]] = {}
        total_valid = total_all = 0.0
        for order in orders:
            amount = order.total_amount
            count, status_amount = by_status.get(order.status.value, (0, 0.0))
            by_status[order.status.value] = (count + 1, round(status_amount + amount, 2))
            total_all = round(total_all + amount, 2)
            if order.status in VALID_SALES_STATUSES:
                total_valid = round(total_valid + amount, 2)
            for item in order.items:
                key = (item.sku, item.name)
                qty, line_amount = items.get(key, (0, 0.0))
                items[key] = (
                    qty + item.quantity,
                    round(line_amount + item.quantity * item.unit_price, 2),
                )
        return CustomerPurchases(
            customer=customer,
            order_count=len(orders),
            total_amount=total_valid,
            total_amount_all=total_all,
            by_status=[
                StatusAmount(status=s, order_count=c, total_amount=a)
                for s, (c, a) in sorted(by_status.items(), key=lambda kv: -kv[1][1])
            ],
            items=[
                PurchaseLine(sku=sku, name=name, total_quantity=q, total_amount=a)
                for (sku, name), (q, a) in sorted(items.items(), key=lambda kv: -kv[1][1])
            ],
        )

    def stock_valuation(self) -> list[StockValuationLine]:
        """库存估值：按品类聚合 Σ数量 × 现价——掌柜算家底。"""
        stmt = (
            select(
                ProductRow.category,
                func.count(func.distinct(ProductRow.sku)).label("sku_count"),
                func.sum(StockRow.quantity).label("total_quantity"),
                func.sum(StockRow.quantity * ProductRow.price).label("total_value"),
            )
            .join(StockRow, StockRow.sku == ProductRow.sku)
            .group_by(ProductRow.category)
            .order_by(ProductRow.category)
        )
        with self._session() as s:
            return [
                StockValuationLine(
                    category=category,
                    sku_count=sku_count,
                    total_quantity=total_quantity,
                    total_value=round(total_value or 0.0, 2),
                )
                for category, sku_count, total_quantity, total_value in s.execute(stmt)
            ]

    def daily_sales(self, *, days: int = 14) -> list[DailySalesPoint]:
        """近 days 天逐日销量（有效口径），按日期升序。"""
        cutoff = datetime.now() - timedelta(days=days)
        stmt = (
            select(
                func.date(OrderRow.created_at).label("d"),
                func.count(func.distinct(OrderRow.order_id)),
                func.sum(OrderItemRow.quantity * OrderItemRow.unit_price),
            )
            .join(OrderItemRow, OrderItemRow.order_id == OrderRow.order_id)
            .where(OrderRow.created_at >= cutoff,
                   OrderRow.status.in_([s.value for s in VALID_SALES_STATUSES]))
            .group_by("d")
            .order_by("d")
        )
        with self._session() as s:
            return [
                DailySalesPoint(
                    date=d, order_count=cnt, total_amount=round(amount or 0.0, 2)
                )
                for d, cnt, amount in s.execute(stmt)
            ]

    def get_stock(self, sku: str) -> StockItem | None:
        with self._session() as s:
            row = s.get(StockRow, sku)
            return (
                StockItem(sku=row.sku, quantity=row.quantity, warehouse=row.warehouse)
                if row
                else None
            )

    # ---- 订单 ----

    def get_order(self, order_id: str) -> Order | None:
        with self._session() as s:
            row = s.get(OrderRow, order_id)
            return _order(row) if row else None

    def list_orders(
        self,
        *,
        status: OrderStatus | None = None,
        customer: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Order]:
        # selectinload：明细第二条查询 IN 批量取，避免每单一查的 N+1；
        # 不用 joinedload 是因为它会破坏 limit/offset 的分页语义
        stmt = (
            _orders_query(status, customer)
            .options(selectinload(OrderRow.items))
            .limit(limit)
            .offset(offset)
        )
        with self._session() as s:
            return [_order(r) for r in s.scalars(stmt)]

    def count_orders(
        self, *, status: OrderStatus | None = None, customer: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(
            _orders_query(status, customer).subquery()
        )
        with self._session() as s:
            return s.scalar(stmt) or 0

    # ---- 报价 ----

    def compute_quote(self, sku: str, quantity: int) -> Quote | None:
        """按现价 × 数量梯度折扣报价；商品不存在或已下架返回 None。

        库存只附带数量不拦截——有价无货时报价依然成立，下单才需要货，
        工具层据此向模型说明"可报价但当前缺货"。
        """
        if quantity < 1:
            return None
        product = self.get_product(sku)
        if product is None or product.status is not ProductStatus.ON_SALE:
            return None
        discount = next((d for threshold, d in QUOTE_TIERS if quantity >= threshold), 1.0)
        unit = round(product.price * discount, 2)
        stock = self.get_stock(sku)
        return Quote(
            sku=product.sku,
            name=product.name,
            unit_price=unit,
            quantity=quantity,
            discount=discount,
            total=round(unit * quantity, 2),
            stock_quantity=stock.quantity if stock else 0,
        )


    # ---- 运营视图（掌柜的日常：盘库存、看销量） ----

    def list_low_stock(self, *, threshold: int = 10, limit: int = 20) -> list[LowStockItem]:
        """库存 ≤ threshold 的商品，按库存升序——盘库存场景。"""
        stmt = (
            select(StockRow, ProductRow)
            .join(ProductRow, ProductRow.sku == StockRow.sku)
            .where(StockRow.quantity <= threshold)
            .order_by(StockRow.quantity.asc(), StockRow.sku)
            .limit(limit)
        )
        with self._session() as s:
            return [
                LowStockItem(
                    sku=p.sku,
                    name=p.name,
                    category=p.category,
                    price=p.price,
                    quantity=st.quantity,
                    warehouse=st.warehouse,
                )
                for st, p in s.execute(stmt)
            ]

    def sales_summary(self, *, days: int = 30) -> SalesSummary:
        """近 days 天有效订单（VALID_SALES_STATUSES）数与金额（快照价口径）。"""
        cutoff = datetime.now() - timedelta(days=days)
        stmt = (
            select(func.count(func.distinct(OrderRow.order_id)),
                   func.sum(OrderItemRow.quantity * OrderItemRow.unit_price))
            .join(OrderItemRow, OrderItemRow.order_id == OrderRow.order_id)
            .where(OrderRow.created_at >= cutoff,
                   OrderRow.status.in_([s.value for s in VALID_SALES_STATUSES]))
        )
        with self._session() as s:
            order_count, total = s.execute(stmt).one()
        return SalesSummary(
            days=days,
            order_count=order_count or 0,
            total_amount=round(total or 0.0, 2),
        )

    def top_products(self, *, days: int = 30, limit: int = 10) -> list[ProductSales]:
        """近 days 天畅销榜，按销量（件数）降序；只统计有效订单。"""
        cutoff = datetime.now() - timedelta(days=days)
        stmt = (
            select(
                OrderItemRow.sku,
                OrderItemRow.name,
                ProductRow.category,
                func.sum(OrderItemRow.quantity).label("total_quantity"),
                func.count(func.distinct(OrderItemRow.order_id)).label("order_count"),
                func.sum(OrderItemRow.quantity * OrderItemRow.unit_price).label("amount"),
            )
            .join(OrderRow, OrderRow.order_id == OrderItemRow.order_id)
            .outerjoin(ProductRow, ProductRow.sku == OrderItemRow.sku)
            .where(OrderRow.created_at >= cutoff,
                   OrderRow.status.in_([s.value for s in VALID_SALES_STATUSES]))
            .group_by(OrderItemRow.sku, OrderItemRow.name, ProductRow.category)
            .order_by(func.sum(OrderItemRow.quantity).desc())
            .limit(limit)
        )
        with self._session() as s:
            return [
                ProductSales(
                    sku=sku,
                    name=name,
                    category=category or "（已下架或已删除）",
                    total_quantity=qty,
                    order_count=cnt,
                    total_amount=round(amount or 0.0, 2),
                )
                for sku, name, category, qty, cnt, amount in s.execute(stmt)
            ]

    def list_categories(self) -> list[CategoryStat]:
        """品类列表与在售商品数——模型探索库时的第一步。"""
        stmt = (
            select(ProductRow.category, func.count())
            .where(ProductRow.status == ProductStatus.ON_SALE.value)
            .group_by(ProductRow.category)
            .order_by(ProductRow.category)
        )
        with self._session() as s:
            return [CategoryStat(category=c, product_count=n) for c, n in s.execute(stmt)]


def _product_query(
    keyword: str | None = None,
    *,
    status: ProductStatus | None = None,
    category: str | None = None,
) -> Select:
    """商品查询的统一过滤器（search / list / count 共用）。"""
    stmt = select(ProductRow).order_by(ProductRow.sku)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(ProductRow.name.like(like), ProductRow.category.like(like)))
    if status is not None:
        stmt = stmt.where(ProductRow.status == status.value)
    if category is not None:
        stmt = stmt.where(ProductRow.category == category)
    return stmt


def _orders_query(
    status: OrderStatus | None, customer: str | None = None
) -> Select:
    stmt = select(OrderRow).order_by(OrderRow.created_at.desc(), OrderRow.order_id)
    if status is not None:
        stmt = stmt.where(OrderRow.status == status.value)
    if customer is not None:
        stmt = stmt.where(OrderRow.customer == customer)
    return stmt


def _product(row: ProductRow) -> Product:
    return Product(
        sku=row.sku,
        name=row.name,
        category=row.category,
        price=row.price,
        status=ProductStatus(row.status),
    )


def _order(row: OrderRow) -> Order:
    return Order(
        order_id=row.order_id,
        customer=row.customer,
        status=OrderStatus(row.status),
        created_at=row.created_at,
        note=row.note,
        items=[
            OrderItem(
                sku=i.sku, name=i.name, quantity=i.quantity, unit_price=i.unit_price
            )
            for i in row.items
        ],
    )
