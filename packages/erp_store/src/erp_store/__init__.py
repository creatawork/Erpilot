"""erp_store：mini-ERP 领域模型与种子数据（M3 第 1 周）。

- 三个模块：商品、库存、订单（含报价计算，见 models / repository）
- 开发期 SQLite（SQLAlchemy，db.py），M6 起生产路径 PostgreSQL + pgvector
- 种子数据有真实感：数百条商品、跨数月订单流水、预埋异常数据
  （零库存 / 已取消 / 已退款 / 含下架商品 / 快照价≠现价）——异常数据是
  边界 case 评测的来源，不是脏数据

用法：
    uv run --package erp-store python -m erp_store seed   # 生成 data/erpilot.db
"""

from erp_store.models import (
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductStatus,
    Quote,
    StockItem,
)
from erp_store.repository import ErpRepository

__all__ = [
    "ErpRepository",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Product",
    "ProductStatus",
    "Quote",
    "StockItem",
]
