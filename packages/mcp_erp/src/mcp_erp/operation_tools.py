"""Business approvals use only saved references on the scoped execution surface."""

from typing import Annotated
from uuid import uuid4

from erp_store.mutations import MutationError
from erp_store.operations import ErpOperations
from pydantic import Field

from mcp_erp.server import _err


def register_operation_tools(mcp, engine, session_id, writes):
    operations = ErpOperations(engine)

    def invoke(operation, *args):
        try:
            return operation(session_id, *args)
        except MutationError as exc:
            return _err(exc.code, exc.message, exc.hint)

    @mcp.tool
    def prepare_cancel_order(order_id: str) -> dict:
        """预览取消订单：客户、原订单金额、状态变化及库存回补；不执行取消或退款。
        取得 operation_id 后才能调用 cancel_order 发起该对象的人工审批。"""
        return invoke(operations.prepare_cancel, order_id)

    @mcp.tool
    def prepare_stock_adjustment(sku: str, delta: Annotated[int, Field(strict=True)]) -> dict:
        """预览库存调整：商品、仓库、增减量和前后数量，不改变库存。
        取得 operation_id 后调用 adjust_stock 发起人工审批。"""
        return invoke(operations.prepare_adjust, sku, delta)

    if writes:

        @mcp.tool
        def cancel_order(operation_id: str, client_token: str | None = None) -> dict:
            """人工审批保存的取消预览；状态/明细/库存变化或过期时不执行，
            必须重新 prepare_cancel_order 并审批。重复同引用只取消一次。不执行退款。"""
            return invoke(
                operations.execute, operation_id, "cancel_order", client_token or uuid4().hex
            )

        @mcp.tool
        def adjust_stock(operation_id: str, client_token: str | None = None) -> dict:
            """人工审批保存的库存影响；库存/仓库/商品名称变化或过期时不执行，
            必须重新 prepare_stock_adjustment 并审批。重复同引用只调整一次。"""
            return invoke(
                operations.execute, operation_id, "adjust_stock", client_token or uuid4().hex
            )
