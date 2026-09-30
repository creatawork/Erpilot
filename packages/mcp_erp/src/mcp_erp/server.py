"""FastMCP Server：把 mini-ERP 的只读查询暴露为 MCP 工具（M3 第 3 周：15 个）。

设计约定（工具卡的完整语义见 docs/tool-cards.md，文档-代码有同步测试把关）：

- 工具**永不返回 None**：查不到返回结构化错误——这是给模型看的信息
- **错误契约 v1**：{"error": {"code", "message", "hint"}}——code 供模型分类
  （not_found / invalid_argument），hint 给出可操作的下一步（用哪个工具、
  传什么参数）；业务校验在工具体内做并返回该结构，而不是靠 schema 约束
  抛协议异常（协议异常到模型手里只剩一句 pydantic 报错，无法自愈）
- 列表类工具返回 {"total", "items"}，让模型知道还有没有下一页
- 参数用 Annotated + Field(description=...)，描述会进 inputSchema

用法：
    库内集成：bridge.build_agent_tools()（agent 侧经 MCP 客户端调用）
    独立进程：python -m mcp_erp serve（stdio，给外部 MCP 客户端用）
"""

from pathlib import Path
from typing import Annotated, Any

from erp_store.db import DEFAULT_DB, make_engine
from erp_store.models import OrderStatus, ProductStatus
from erp_store.repository import ErpRepository
from fastmcp import FastMCP
from pydantic import Field

_ORDER_STATUS_HELP = " / ".join(s.value for s in OrderStatus)
_PRODUCT_STATUS_HELP = " / ".join(s.value for s in ProductStatus)


def _err(code: str, message: str, hint: str = "") -> dict[str, Any]:
    """错误契约 v1：code 供分类，message 说明发生了什么，hint 指下一步。"""
    err: dict[str, Any] = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    return {"error": err}


def _not_found(what: str, key: str, hint: str) -> dict[str, Any]:
    return _err("not_found", f"{what}不存在：{key}", hint)


def _parse_status(value: str, enum_cls, help_text: str) -> tuple[Any, dict[str, Any] | None]:
    try:
        return enum_cls(value), None
    except ValueError:
        return None, _err("invalid_argument", f"无效取值：{value}", f"可选：{help_text}")


def create_server(db_path: Path = DEFAULT_DB) -> FastMCP:
    """构建 MCP server 实例（库内集成与独立进程共用）。"""
    repo = ErpRepository(make_engine(Path(db_path)))
    mcp = FastMCP(
        name="erpilot-erp",
        instructions=(
            "Erpilot mini-ERP 只读工具集（商品/库存/订单/报价）。"
            '查不到时返回 {"error": {"code", "message", "hint"}}——按 hint 换工具'
            "或向用户要更多信息；列表类返回 {\"total\", \"items\"}。"
            "下单/改库存等写操作当前未开放。"
        ),
    )

    # ---- 订单 ----

    @mcp.tool
    def get_order(
        order_id: Annotated[
            str, Field(description="订单号，格式 SO+日期+序号，如 SO20260301-0001")
        ],
    ) -> dict[str, Any]:
        """按订单号查询订单：状态、客户、明细与金额（金额按下单快照价）。"""
        order = repo.get_order(order_id)
        if order is None:
            return _not_found(
                "订单", order_id,
                "订单号为 SO+日期+序号 格式；可用 list_orders 浏览现有订单，"
                "或请用户提供完整单号",
            )
        return order.model_dump(mode="json")

    @mcp.tool
    def list_orders(
        status: Annotated[
            str | None, Field(description=f"按状态过滤，可选：{_ORDER_STATUS_HELP}")
        ] = None,
        customer: Annotated[str | None, Field(description="按客户名精确过滤")] = None,
        limit: Annotated[int, Field(description="每页条数（1~100）")] = 20,
        offset: Annotated[int, Field(description="跳过条数（翻页用）")] = 0,
    ) -> dict[str, Any]:
        """按时间倒序列订单（带 total，供翻页判断）。"""
        if not 1 <= limit <= 100 or offset < 0:
            return _err("invalid_argument", "limit 须在 1~100，offset 须 ≥ 0")
        parsed, err = (None, None)
        if status:
            parsed, err = _parse_status(status, OrderStatus, _ORDER_STATUS_HELP)
            if err:
                return err
        orders = repo.list_orders(status=parsed, customer=customer, limit=limit, offset=offset)
        return {
            "total": repo.count_orders(status=parsed, customer=customer),
            "items": [o.model_dump(mode="json") for o in orders],
        }

    @mcp.tool
    def get_orders_by_sku(
        sku: Annotated[str, Field(description="商品 SKU")],
        limit: Annotated[int, Field(description="最多返回条数（1~100）")] = 20,
    ) -> dict[str, Any]:
        """反查某 SKU 出现在哪些订单里（含全部状态，按下单时间倒序）——
        "这个商品都卖给谁了 / 进了哪些单"场景。"""
        if not 1 <= limit <= 100:
            return _err("invalid_argument", "limit 须在 1~100")
        orders = repo.get_orders_by_sku(sku, limit=limit)
        return {
            "total": repo.count_orders_by_sku(sku),
            "items": [o.model_dump(mode="json") for o in orders],
        }

    # ---- 商品 ----

    @mcp.tool
    def get_product(
        sku: Annotated[str, Field(description="商品 SKU，如 A1001")],
    ) -> dict[str, Any]:
        """按 SKU 查商品：名称、品类、现价、在售状态。"""
        product = repo.get_product(sku)
        if product is None:
            return _not_found(
                "商品", sku, "可用 search_products 按名称/品类关键词查找 SKU"
            )
        return product.model_dump(mode="json")

    @mcp.tool
    def search_products(
        keyword: Annotated[str, Field(description="名称/品类的关键词，如 青瓷")],
        limit: Annotated[int, Field(description="最多返回条数（1~100）")] = 20,
    ) -> dict[str, Any]:
        """按关键词搜商品，返回 {"total", "items"}。"""
        if not 1 <= limit <= 100:
            return _err("invalid_argument", "limit 须在 1~100")
        kw = keyword.strip()
        if not kw:
            return _err("invalid_argument", "关键词不能为空", "例如：青瓷、宣纸、香道")
        hits = repo.search_products(kw, limit=limit)
        return {
            "total": repo.count_products(kw),
            "items": [p.model_dump(mode="json") for p in hits],
        }

    @mcp.tool
    def list_products(
        status: Annotated[
            str | None, Field(description=f"按状态过滤，可选：{_PRODUCT_STATUS_HELP}")
        ] = None,
        category: Annotated[
            str | None, Field(description="按品类精确过滤，可先调 list_categories")
        ] = None,
        limit: Annotated[int, Field(description="每页条数（1~100）")] = 20,
        offset: Annotated[int, Field(description="跳过条数（翻页用）")] = 0,
    ) -> dict[str, Any]:
        """商品列表：按状态/品类组合过滤（不带关键词的浏览入口）。"""
        if not 1 <= limit <= 100 or offset < 0:
            return _err("invalid_argument", "limit 须在 1~100，offset 须 ≥ 0")
        parsed, err = (None, None)
        if status:
            parsed, err = _parse_status(status, ProductStatus, _PRODUCT_STATUS_HELP)
            if err:
                return err
        products = repo.list_products(
            status=parsed, category=category, limit=limit, offset=offset
        )
        return {
            "total": repo.count_products(status=parsed, category=category),
            "items": [p.model_dump(mode="json") for p in products],
        }

    # ---- 库存 / 报价 ----

    @mcp.tool
    def get_stock(
        sku: Annotated[str, Field(description="商品 SKU")],
    ) -> dict[str, Any]:
        """按 SKU 查当前库存数量与仓库。"""
        stock = repo.get_stock(sku)
        if stock is None:
            return _not_found("库存记录", sku, "先用 search_products 确认 SKU 是否存在")
        return stock.model_dump(mode="json")

    @mcp.tool
    def compute_quote(
        sku: Annotated[str, Field(description="商品 SKU")],
        quantity: Annotated[int, Field(description="数量；≥10/50/200 件有梯度折扣，至少为 1")],
    ) -> dict[str, Any]:
        """按现价与数量梯度折扣报价；附带当前库存（为 0 时报价仅参考）。"""
        if quantity < 1:
            return _err("invalid_argument", f"数量至少为 1（收到 {quantity}）")
        quote = repo.compute_quote(sku, quantity)
        if quote is None:
            return _not_found(
                "可报价商品（不存在或已下架）", sku,
                "已下架商品不可报价；可用 search_products 换在售商品",
            )
        return quote.model_dump(mode="json")

    @mcp.tool
    def compare_quotes(
        skus: Annotated[list[str], Field(description="SKU 列表（2~10 个，同数量比价）")],
        quantity: Annotated[int, Field(description="统一数量；≥10/50/200 件有梯度折扣")],
    ) -> dict[str, Any]:
        """批量报价并按总价升序——多商品比价场景。"""
        if not 2 <= len(skus) <= 10:
            return _err("invalid_argument", "skus 须为 2~10 个")
        if quantity < 1:
            return _err("invalid_argument", f"数量至少为 1（收到 {quantity}）")
        quotes, unavailable = [], []
        for sku in dict.fromkeys(skus):  # 去重且保序
            quote = repo.compute_quote(sku, quantity)
            if quote is None:
                unavailable.append(sku)
            else:
                quotes.append(quote.model_dump(mode="json"))
        quotes.sort(key=lambda q: q["total"])
        return {"quantity": quantity, "quotes": quotes, "unavailable": unavailable}

    @mcp.tool
    def list_low_stock(
        threshold: Annotated[int, Field(description="库存预警线（≤ 该值算低库存）")] = 10,
        limit: Annotated[int, Field(description="最多返回条数（1~100）")] = 20,
    ) -> dict[str, Any]:
        """列出低库存商品（按库存升序）——盘库存、补货建议场景。"""
        if not 0 <= threshold <= 10_000 or not 1 <= limit <= 100:
            return _err("invalid_argument", "threshold 须在 0~10000，limit 须在 1~100")
        rows = repo.list_low_stock(threshold=threshold, limit=limit)
        return [i.model_dump(mode="json") for i in rows]

    # ---- 运营视图 ----

    @mcp.tool
    def sales_summary(
        days: Annotated[int, Field(description="统计最近 N 天（1~365）")] = 30,
    ) -> dict[str, Any]:
        """近 N 天销量汇总：有效订单数（待发货/已发货/已签收）与总金额。"""
        if not 1 <= days <= 365:
            return _err("invalid_argument", "days 须在 1~365")
        return repo.sales_summary(days=days).model_dump(mode="json")

    @mcp.tool
    def top_products(
        days: Annotated[int, Field(description="统计最近 N 天（1~365）")] = 30,
        limit: Annotated[int, Field(description="榜单长度（1~50）")] = 10,
    ) -> dict[str, Any]:
        """近 N 天畅销榜（按销量降序），只统计有效订单。"""
        if not 1 <= days <= 365 or not 1 <= limit <= 50:
            return _err("invalid_argument", "days 须在 1~365，limit 须在 1~50")
        return [p.model_dump(mode="json") for p in repo.top_products(days=days, limit=limit)]

    @mcp.tool
    def daily_sales(
        days: Annotated[int, Field(description="统计最近 N 天（1~90）")] = 14,
    ) -> dict[str, Any]:
        """近 N 天逐日销量点（有效口径）——看趋势、找异常日。"""
        if not 1 <= days <= 90:
            return _err("invalid_argument", "days 须在 1~90")
        return [p.model_dump(mode="json") for p in repo.daily_sales(days=days)]

    @mcp.tool
    def stock_valuation() -> dict[str, Any]:
        """库存估值：按品类的库存数量与金额（现价口径），掌柜算家底。"""
        lines = repo.stock_valuation()
        return {
            "grand_total_value": round(sum(line.total_value for line in lines), 2),
            "categories": [line.model_dump(mode="json") for line in lines],
        }

    @mcp.tool
    def list_categories() -> dict[str, Any]:
        """列出全部品类与在售商品数——探索库存时的第一步。"""
        cats = repo.list_categories()
        return {"total": len(cats), "items": [c.model_dump(mode="json") for c in cats]}

    return mcp
