"""写操作 API：M4 写工具（mcp_erp）的唯一写入口（ADR-0005 决策 1）。

与只读 repository 刻意分层——写入口显式、可枚举、可审计。领域规则在这里
执行，错误以 MutationError（code/message/hint，错误契约 v1 的写路径延伸）
抛出，MCP 工具层负责转成 {"error": {...}} 回填给模型：

- create_order：商品须在售、库存须足量；快照价取现价，扣库存与建单同事务
- cancel_order：仅待付款/待发货可取消（状态机），取消回补库存
- adjust_stock：库存不为负
- set_product_status：目标状态与现状一致返回 invalid_transition——空操作
  报成功是在训练模型说谎（ADR-0005）
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from erp_store.db import MutationRequestRow, OrderItemRow, OrderRow, ProductRow, StockRow
from erp_store.models import Order, OrderStatus, ProductStatus


class MutationError(Exception):
    """写操作的业务错误：code 供模型分类，hint 给可操作的下一步。"""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class TokenLookup:
    """按 token 查幂等表的结果（T06，ADR-0008 恢复契约）。

    status 三态：found（业务已提交，result 是首次成功结果）/
    not_found（查询成功但无记录——不单独证明未执行）/ conflict（同 token
    异参，原批准不可迁移）。查询本身失败以异常向上抛，由调用方判 unknown。
    """

    status: str
    result: Any = None
    request: list | None = None


# 允许取消的状态（状态机 v1：已发货/已签收走退款流程，不在本工具范围）
CANCELLABLE_STATUSES = (OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING_SHIPMENT)


class ErpMutations:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_order(
        self,
        customer: str,
        items: list[tuple[str, int]],
        *,
        note: str | None = None,
        client_token: str | None = None,
    ) -> Order:
        """建单：快照价取现价，校验在售与库存，扣减与建单同事务。

        items 为 (sku, quantity) 列表，同 SKU 自动合并数量；新订单从
        待付款起步（收款确认后走发货，本工具面不管收款）。
        """
        if not customer.strip():
            raise MutationError(
                "invalid_argument", "客户名不能为空", "请提供下单客户的全名"
            )
        if not items:
            raise MutationError(
                "invalid_argument", "订单至少要有一行商品", "请提供 (SKU, 数量) 列表"
            )
        merged: dict[str, int] = {}
        for sku, quantity in items:
            if quantity < 1:
                raise MutationError(
                    "invalid_argument",
                    f"商品 {sku} 的数量至少为 1（收到 {quantity}）",
                )
            merged[sku] = merged.get(sku, 0) + quantity

        request = ["create_order", customer.strip(), sorted(merged.items()), note]
        with self._write_session() as s:
            prior = self._prior(s, client_token, request)
            if prior is not None:
                return Order.model_validate(prior)
            products = {
                p.sku: p
                for p in s.scalars(
                    select(ProductRow).where(ProductRow.sku.in_(merged))
                )
            }
            missing = [sku for sku in merged if sku not in products]
            if missing:
                raise MutationError(
                    "not_found",
                    f"商品不存在：{'、'.join(missing)}",
                    "可用 search_products 按名称/品类确认 SKU",
                )
            off_sale = [
                sku for sku, p in products.items()
                if p.status != ProductStatus.ON_SALE.value
            ]
            if off_sale:
                raise MutationError(
                    "invalid_transition",
                    f"商品已下架，不可下单：{'、'.join(off_sale)}",
                    "已下架商品需先上架（set_product_status）或换在售商品",
                )
            stocks = {
                st.sku: st
                for st in s.scalars(select(StockRow).where(StockRow.sku.in_(merged)))
            }
            for sku, quantity in merged.items():
                stock = stocks.get(sku)
                have = stock.quantity if stock else 0
                if have < quantity:
                    raise MutationError(
                        "insufficient_stock",
                        f"商品 {products[sku].name}（{sku}）库存不足：需 {quantity}，"
                        f"当前 {have}",
                        "可减少数量或改报其他在售商品；库存数据可用 get_stock 复核",
                    )
            now = datetime.now()
            order = OrderRow(
                order_id=self._next_order_id(s, now),
                customer=customer.strip(),
                status=OrderStatus.PENDING_PAYMENT.value,
                created_at=now,
                note=note,
                items=[
                    OrderItemRow(
                        sku=sku,
                        name=products[sku].name,
                        quantity=quantity,
                        unit_price=products[sku].price,  # 快照价 = 下单时现价
                    )
                    for sku, quantity in merged.items()
                ],
            )
            for sku, quantity in merged.items():
                stocks[sku].quantity -= quantity
            s.add(order)
            s.flush()
            result = self._load_order(s, order.order_id)
            self._remember(s, client_token, request, result.model_dump(mode="json"))
            s.commit()
            return result

    def cancel_order(self, order_id: str, *, client_token: str | None = None) -> Order:
        """取消订单：仅待付款/待发货可取消（状态机），取消回补库存。"""
        request = ["cancel_order", order_id]
        with self._write_session() as s:
            prior = self._prior(s, client_token, request)
            if prior is not None:
                return Order.model_validate(prior)
            order = s.get(OrderRow, order_id)
            if order is None:
                raise MutationError(
                    "not_found",
                    f"订单不存在：{order_id}",
                    "订单号为 SO+日期+序号 格式；可用 list_orders 浏览现有订单",
                )
            status = OrderStatus(order.status)
            if status not in CANCELLABLE_STATUSES:
                raise MutationError(
                    "invalid_transition",
                    f"订单 {order_id} 当前状态为「{status.value}」，不可取消",
                    "仅待付款/待发货可取消；已发货/已签收走退款流程（工具面未开放）",
                )
            order.status = OrderStatus.CANCELLED.value
            for item in order.items:  # 回补库存；商品可能已被删，逐行容错
                stock = s.get(StockRow, item.sku)
                if stock is not None:
                    stock.quantity += item.quantity
            s.flush()
            result = self._load_order(s, order_id)
            self._remember(s, client_token, request, result.model_dump(mode="json"))
            s.commit()
            return result

    def adjust_stock(
        self, sku: str, delta: int, *, client_token: str | None = None
    ) -> tuple[str, int]:
        """库存增减（delta 正入负出），返回 (sku, 调整后数量)。库存不为负。"""
        if delta == 0:
            raise MutationError("invalid_argument", "调整量不能为 0")
        request = ["adjust_stock", sku, delta]
        with self._write_session() as s:
            prior = self._prior(s, client_token, request)
            if prior is not None:
                return tuple(prior)
            product = s.get(ProductRow, sku)
            if product is None:
                raise MutationError(
                    "not_found",
                    f"商品不存在：{sku}",
                    "可用 search_products 按名称/品类确认 SKU",
                )
            stock = s.get(StockRow, sku)
            current = stock.quantity if stock else 0
            new_quantity = current + delta
            if new_quantity < 0:
                raise MutationError(
                    "insufficient_stock",
                    f"商品 {product.name}（{sku}）当前库存 {current}，"
                    f"不能减 {abs(delta)}（会变为负数）",
                    "出库量不能超过当前库存；可先用 get_stock 复核",
                )
            if stock is None:
                stock = StockRow(sku=sku, quantity=new_quantity)
                s.add(stock)
            else:
                stock.quantity = new_quantity
            self._remember(s, client_token, request, [sku, new_quantity])
            s.commit()
            return sku, new_quantity

    def set_product_status(
        self, sku: str, status: ProductStatus, *, client_token: str | None = None
    ) -> str:
        """商品上下架；目标状态与现状一致返回 invalid_transition。"""
        request = ["set_product_status", sku, status.value]
        with self._write_session() as s:
            prior = self._prior(s, client_token, request)
            if prior is not None:
                return prior
            product = s.get(ProductRow, sku)
            if product is None:
                raise MutationError(
                    "not_found",
                    f"商品不存在：{sku}",
                    "可用 search_products 按名称/品类确认 SKU",
                )
            if product.status == status.value:
                raise MutationError(
                    "invalid_transition",
                    f"商品 {product.name}（{sku}）已是「{status.value}」状态，无需变更",
                    "空操作不执行；可用 get_product 复核当前状态",
                )
            product.status = status.value
            self._remember(s, client_token, request, sku)
            s.commit()
            return sku

    def lookup_token(self, client_token: str) -> TokenLookup:
        """内部恢复查询（T06，ADR-0008 §3 推演 3）：按 token 查幂等表。

        不进模型工具面（不增加可见工具数量）；查询失败（DB 错/超时）以异常
        向上抛，由恢复协调判 unknown——禁止在查询失败时做任何重试决策。
        """
        if not client_token or not client_token.strip() or len(client_token) > 128:
            raise MutationError("invalid_argument", "幂等键须为 1~128 个非空字符")
        with self._write_session() as s:
            row = s.get(MutationRequestRow, client_token)
            if row is None:
                return TokenLookup("not_found")
            return TokenLookup("found", json.loads(row.result), json.loads(row.request))

    @staticmethod
    def canonical_request(tool: str, arguments: dict) -> list:
        """工具入参 → 幂等表 request 口径（与各 mutation 的 request 构造一致）。

        恢复对账用：同 token 异参时 row.request 与本口径不一致即 conflict。
        """
        if tool == "adjust_stock":
            return ["adjust_stock", arguments["sku"], int(arguments["delta"])]
        if tool == "cancel_order":
            return ["cancel_order", arguments["order_id"]]
        if tool == "set_product_status":
            # 状态入参可能是别名或枚举值，统一归一到幂等表存的枚举 value
            return [
                "set_product_status", arguments["sku"],
                ProductStatus(arguments["status"]).value,
            ]
        if tool == "create_order":
            merged: dict[str, int] = {}
            for item in arguments["items"]:
                merged[item["sku"]] = merged.get(item["sku"], 0) + int(item["quantity"])
            note = arguments.get("note")
            return [
                "create_order", arguments["customer"].strip(),
                [[sku, quantity] for sku, quantity in sorted(merged.items())], note,
            ]
        raise ValueError(f"工具 {tool} 没有幂等 request 口径")

    @contextmanager
    def _write_session(self):
        # SQLite 单写者：在首次读取前抢写锁，避免库存/状态/单号的读改写竞争。
        # 新表在写锁内按需创建，旧种子库无需重建；DDL 与写结果一起提交。
        with Session(self._engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            MutationRequestRow.__table__.create(session.connection(), checkfirst=True)
            yield session

    def _prior(self, session: Session, token: str | None, request: list):
        if token is None:
            return None
        if not token.strip() or len(token) > 128:
            raise MutationError("invalid_argument", "幂等键须为 1~128 个非空字符")
        row = session.get(MutationRequestRow, token)
        if row is None:
            return None
        if row.request != self._request_json(request):
            raise MutationError(
                "idempotency_conflict", "幂等键已用于不同写请求",
                "重试原请求须保留原参数；新操作请使用新的 client_token",
            )
        return json.loads(row.result)

    def _remember(self, session: Session, token: str | None, request: list, result) -> None:
        if token is not None:
            session.add(MutationRequestRow(
                client_token=token, request=self._request_json(request),
                result=json.dumps(result, ensure_ascii=False, sort_keys=True),
            ))

    @staticmethod
    def _request_json(request: list) -> str:
        return json.dumps(request, ensure_ascii=False, sort_keys=True)

    def _next_order_id(self, s: Session, now: datetime) -> str:
        """SO+日期+序号：取当日最大序号 +1（与种子数据的单号格式一致）。"""
        prefix = f"SO{now:%Y%m%d}-"
        rows = s.scalars(
            select(OrderRow.order_id).where(OrderRow.order_id.like(prefix + "%"))
        )
        max_seq = 0
        for order_id in rows:
            try:
                max_seq = max(max_seq, int(order_id.removeprefix(prefix)))
            except ValueError:
                continue
        return f"{prefix}{max_seq + 1:04d}"

    def _load_order(self, s: Session, order_id: str) -> Order:
        """提交后重查再转模型（commit 会过期 ORM 对象）。"""
        from erp_store.repository import _order

        row = s.get(OrderRow, order_id)
        assert row is not None
        return _order(row)
