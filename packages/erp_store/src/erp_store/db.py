"""SQLAlchemy 持久化：SQLite 起步，M6 换 PostgreSQL 只动 engine/URL（既定路径）。

表结构与 models.py 的 Pydantic 模型一一对应。订单金额不入库——由订单项
推导（models.Order.total_amount），快照价与汇总永不打架。
"""

from datetime import datetime
from pathlib import Path

from sqlalchemy import Float, ForeignKey, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

DEFAULT_DB = Path("data/erpilot.db")


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))


class StockRow(Base):
    __tablename__ = "stocks"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column()
    warehouse: Mapped[str] = mapped_column(String(32), default="主仓")


class OrderRow(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    items: Mapped[list["OrderItemRow"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItemRow.id",
    )


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column()
    unit_price: Mapped[float] = mapped_column(Float)

    order: Mapped[OrderRow] = relationship(back_populates="items")


class MutationRequestRow(Base):
    """成功写请求的持久化幂等记录；与业务变更在同一事务提交。"""

    __tablename__ = "mutation_requests"

    client_token: Mapped[str] = mapped_column(String(128), primary_key=True)
    request: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)


def make_engine(db_path: Path = DEFAULT_DB) -> Engine:
    """SQLite 引擎。

    check_same_thread=False：连接池里的连接会被不同线程取用（M3 工具层
    跑在 FastMCP/FastAPI 的工作线程里），SQLite 的同线程限制交给连接池
    与文件锁兜底；M6 换 PostgreSQL 后该参数随之消失。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
