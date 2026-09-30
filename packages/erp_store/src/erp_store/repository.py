"""只读查询 API：M3 工具层（mcp_erp）取数的唯一入口。

写操作刻意不在此层——动账动货要请示东家（HITL，M4+），接口先不给。
所有查询返回 models.py 的 Pydantic 模型，工具层拿到的直接是可序列化对象。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Select, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from erp_store.db import OrderRow, ProductRow, StockRow
from erp_store.models import (
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductStatus,
    Quote,
    StockItem,
)

# 数量门槛 → 折扣，从高到低取第一个命中的档
QUOTE_TIERS: list[tuple[int, float]] = [(200, 0.90), (50, 0.95), (10, 0.98)]


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
        like = f"%{kw}%"
        stmt = (
            select(ProductRow)
            .where(or_(ProductRow.name.like(like), ProductRow.category.like(like)))
            .order_by(ProductRow.sku)
            .limit(limit)
        )
        with self._session() as s:
            return [_product(r) for r in s.scalars(stmt)]

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
        stmt = select(func.count()).select_from(_orders_query(status).subquery())
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
