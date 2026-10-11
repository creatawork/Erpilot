"""Draft tools bind ownership outside the model-visible argument schema."""

from typing import Annotated

from erp_store.drafts import ErpDrafts
from erp_store.mutations import MutationError
from pydantic import Field

from mcp_erp.server import OrderItemInput, _err


def register_draft_tools(mcp, engine, session_id: str, writes: bool):
    drafts = ErpDrafts(engine)

    def invoke(operation, *args, **kwargs):
        try:
            return operation(session_id, *args, **kwargs)
        except MutationError as exc:
            return _err(exc.code, exc.message, exc.hint)

    @mcp.tool
    def prepare_order_draft(
        customer: str, items: list[OrderItemInput], note: str | None = None
    ) -> dict:
        """创建报价草稿，不占库存；返回客户、明细、折扣、总价及 30 分钟有效期。"""
        return invoke(drafts.prepare, customer, [(i.sku, i.quantity) for i in items], note)

    @mcp.tool
    def get_order_draft(draft_id: str, revision: int | None = None) -> dict:
        """查询当前会话草稿；可指定版本查看不可修改的报价快照。"""
        return invoke(drafts.get, draft_id, revision)

    @mcp.tool
    def revise_order_draft(
        draft_id: str,
        expected_revision: int,
        customer: str,
        items: list[OrderItemInput],
        note: str | None = None,
    ) -> dict:
        """修改或刷新草稿，生成新版本；旧版本审批失效，须重新确认。"""
        return invoke(
            drafts.revise,
            draft_id,
            expected_revision,
            customer,
            [(i.sku, i.quantity) for i in items],
            note,
        )

    if writes:

        @mcp.tool
        def submit_order_draft(
            draft_id: str, revision: Annotated[int, Field(ge=1)], client_token: str | None = None
        ) -> dict:
            """人工批准指定草稿版本后提交；价格、库存、在售状态变化或过期时拒绝，
            须刷新草稿重新审批，禁止自动提交新版本。重复提交只生成一张订单。"""
            from uuid import uuid4

            result = invoke(drafts.submit, draft_id, revision, client_token or uuid4().hex)
            if isinstance(result, dict):
                return result
            return {
                **result.model_dump(mode="json"),
                "result_note": "订单已创建，当前状态「待付款」；金额按下单快照价",
            }
