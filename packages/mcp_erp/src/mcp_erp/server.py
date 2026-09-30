"""FastMCP Server：把 mini-ERP 的只读查询暴露为 MCP 工具（M3 第 2 周，首批 10 个）。

设计约定（工具设计精研在 M3 第 3 周继续）：

- 工具**永不返回 None**：查不到返回 {"error": ...} 结构化错误——这是给模型
  看的信息，与 agent_core 的错误回填格式一致；None 在 MCP/JSON 里信息量为零
- 列表类工具返回 {"total": n, "items": [...]}，让模型知道还有没有下一页
- 参数用 Annotated + Field(description=...)，描述会进 inputSchema，
  是模型选择工具的唯一依据
- 状态参数收 str（MCP schema 里是普通字符串），在工具内校验并给出
  可选值提示，不把枚举校验错误直接抛给模型

用法：
    库内集成：bridge.build_agent_tools()（agent 侧经 MCP 客户端调用）
    独立进程：python -m mcp_erp serve（stdio，给外部 MCP 客户端用）
"""

from pathlib import Path
from typing import Annotated, Any

from erp_store.db import DEFAULT_DB, make_engine
from erp_store.models import OrderStatus
from erp_store.repository import ErpRepository
from fastmcp import FastMCP
from pydantic import Field

_ORDER_STATUS_HELP = " / ".join(s.value for s in OrderStatus)


def _not_found(what: str, key: str) -> dict[str, Any]:
    return {"error": f"{what}不存在：{key}"}


def _bad_status(value: str) -> dict[str, Any]:
    return {"error": f"无效的订单状态：{value}（可选：{_ORDER_STATUS_HELP}）"}


def create_server(db_path: Path = DEFAULT_DB) -> FastMCP:
    """构建 MCP server 实例（库内集成与独立进程共用）。"""
    repo = ErpRepository(make_engine(Path(db_path)))
    mcp = FastMCP(
        name="erpilot-erp",
        instructions=(
            "Erpilot mini-ERP 只读工具集（商品/库存/订单/报价）。"
            "查不到时返回 {\"error\": ...}；列表类返回 {\"total\", \"items\"}。"
            "下单/改库存等写操作当前未开放。"
        ),
    )

    @mcp.tool
    def get_order(
        order_id: Annotated[str, Field(description="订单号，如 SO20260301-0001")],
    ) -> dict[str, Any]:
        """按订单号查询订单：状态、客户、明细与金额（金额按下单快照价）。"""
        order = repo.get_order(order_id)
        return order.model_dump(mode="json") if order else _not_found("订单", order_id)

    @mcp.tool
    def list_orders(
        status: Annotated[
            str | None, Field(description=f"按状态过滤，可选：{_ORDER_STATUS_HELP}")
        ] = None,
        customer: Annotated[str | None, Field(description="按客户名精确过滤")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="每页条数")] = 20,
        offset: Annotated[int, Field(ge=0, description="跳过条数（翻页用）")] = 0,
    ) -> dict[str, Any]:
        """按时间倒序列订单（带 total，供翻页判断）。"""
        try:
            parsed = OrderStatus(status) if status else None
        except ValueError:
            return _bad_status(status)
        orders = repo.list_orders(status=parsed, customer=customer, limit=limit, offset=offset)
        return {
            "total": repo.count_orders(status=parsed, customer=customer),
            "items": [o.model_dump(mode="json") for o in orders],
        }

    @mcp.tool
    def get_product(
        sku: Annotated[str, Field(description="商品 SKU，如 A1001")],
    ) -> dict[str, Any]:
        """按 SKU 查商品：名称、品类、现价、在售状态。"""
        product = repo.get_product(sku)
        return product.model_dump(mode="json") if product else _not_found("商品", sku)

    @mcp.tool
    def search_products(
        keyword: Annotated[str, Field(description="名称/品类的关键词，如 青瓷")],
        limit: Annotated[int, Field(ge=1, le=100, description="最多返回条数")] = 20,
    ) -> dict[str, Any]:
        """按关键词搜商品，返回 {"total", "items"}。"""
        hits = repo.search_products(keyword, limit=limit)
        return {
            "total": repo.count_products(keyword),
            "items": [p.model_dump(mode="json") for p in hits],
        }

    @mcp.tool
    def get_stock(
        sku: Annotated[str, Field(description="商品 SKU")],
    ) -> dict[str, Any]:
        """按 SKU 查当前库存数量与仓库。"""
        stock = repo.get_stock(sku)
        return stock.model_dump(mode="json") if stock else _not_found("库存记录", sku)

    @mcp.tool
    def compute_quote(
        sku: Annotated[str, Field(description="商品 SKU")],
        quantity: Annotated[int, Field(ge=1, description="数量；≥10/50/200 件有梯度折扣")],
    ) -> dict[str, Any]:
        """按现价与数量梯度折扣报价；附带当前库存（为 0 时报价仅参考）。"""
        quote = repo.compute_quote(sku, quantity)
        return quote.model_dump(mode="json") if quote else _not_found(
            "可报价商品（不存在或已下架）", sku
        )

    @mcp.tool
    def list_low_stock(
        threshold: Annotated[int, Field(ge=0, description="库存预警线（≤ 该值算低库存）")] = 10,
        limit: Annotated[int, Field(ge=1, le=100, description="最多返回条数")] = 20,
    ) -> dict[str, Any]:
        """列出低库存商品（按库存升序）——盘库存、补货建议场景。"""
        rows = repo.list_low_stock(threshold=threshold, limit=limit)
        return [i.model_dump(mode="json") for i in rows]

    @mcp.tool
    def sales_summary(
        days: Annotated[int, Field(ge=1, description="统计最近 N 天")] = 30,
    ) -> dict[str, Any]:
        """近 N 天销量汇总：有效订单数（待发货/已发货/已签收）与总金额。"""
        return repo.sales_summary(days=days).model_dump(mode="json")

    @mcp.tool
    def top_products(
        days: Annotated[int, Field(ge=1, description="统计最近 N 天")] = 30,
        limit: Annotated[int, Field(ge=1, le=50, description="榜单长度")] = 10,
    ) -> dict[str, Any]:
        """近 N 天畅销榜（按销量降序），只统计有效订单。"""
        return [p.model_dump(mode="json") for p in repo.top_products(days=days, limit=limit)]

    @mcp.tool
    def list_categories() -> dict[str, Any]:
        """列出全部品类与在售商品数——探索库存时的第一步。"""
        cats = repo.list_categories()
        return {"total": len(cats), "items": [c.model_dump(mode="json") for c in cats]}

    return mcp
