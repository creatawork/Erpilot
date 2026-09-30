"""领域模型：商品 / 库存 / 订单（含报价计算）。

Pydantic 模型是对外（M3 工具层、API、前端）的唯一形态；SQLAlchemy 表
（db.py）只是持久化形态，两侧字段一一对应。

v1 金额用 float 并在生成边界 round 保留两位——真实 ERP 该用 Decimal，
评测阶段若出现精度问题再切换（本注释即决策记录，不是疏忽）。
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ProductStatus(StrEnum):
    ON_SALE = "在售"
    OFF_SALE = "已下架"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "待付款"
    PENDING_SHIPMENT = "待发货"
    SHIPPED = "已发货"
    DELIVERED = "已签收"
    CANCELLED = "已取消"
    REFUNDED = "已退款"


class Product(BaseModel):
    sku: str
    name: str
    category: str
    price: float = Field(gt=0, description="现价（元）；报价用现价")
    status: ProductStatus = ProductStatus.ON_SALE


class StockItem(BaseModel):
    sku: str
    quantity: int = Field(ge=0, description="库存不为负——超卖是订单层的事")
    warehouse: str = "主仓"


class OrderItem(BaseModel):
    sku: str
    name: str
    quantity: int = Field(ge=1)
    unit_price: float = Field(gt=0, description="下单时快照价，可能与现价不同")


class Order(BaseModel):
    order_id: str
    customer: str
    status: OrderStatus
    created_at: datetime
    items: list[OrderItem]
    note: str | None = None

    @property
    def total_amount(self) -> float:
        """按快照价汇总——订单金额永远由订单项推导，不单独入库。"""
        return round(sum(i.quantity * i.unit_price for i in self.items), 2)


class Quote(BaseModel):
    """报价 = 现价 × 数量 × 数量梯度折扣（见 repository.QUOTE_TIERS）。"""

    sku: str
    name: str
    unit_price: float = Field(description="现价 × 折扣后的单件价")
    quantity: int
    discount: float = Field(description="1.0 表示无折扣")
    total: float
    stock_quantity: int = Field(description="当前库存；为 0 时报价仅参考")
