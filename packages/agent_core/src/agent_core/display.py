"""业务结果展示适配器（设计 5.2）：把工具原始结果转换成版本化 display 字段。

只覆盖有三类明确契约的工具：库存查询、库存调整、订单创建；其余工具返回 None，
前端回退通用详情。契约来源（MCP server/bridge 实际返回，2026-10 校核）：
- get_stock / check_stock → {"sku", "quantity"/"stock", "warehouse"/"available"} 或 {"error": ...}
- adjust_stock → {"sku", "quantity", "note"} 或 {"error": ...}
- create_order → Order dump {order_id, customer, status, items, total_amount, ...} 或 {"error": ...}
- 审批拒绝 → {"approval": "denied", "message", "note"}（transport ok=True）

outcome 分类：denied 优先；transport ok、业务 error 字段共同决定 succeeded/failed；
无法确认时 unknown。适配器校验失败不影响业务操作，只回退详情并记诊断。
"""

import json
import logging
from typing import Any

logger = logging.getLogger("agent_core.display")

DISPLAY_VERSION = 1

_BUILDERS = {}


def build_display(
    name: str, arguments: str | dict, content: str, ok: bool
) -> dict[str, Any] | None:
    """按工具名构建 display；未知工具或契约不符返回 None（不抛出）。"""
    builder = _BUILDERS.get(name)
    if builder is None:
        return None
    try:
        args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        if not isinstance(args, dict):
            return None
        try:
            result = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            result = None
        if not ok:
            # 传输失败先于内容解析：结果内容不可信，不进 unknown
            return _display(builder["kind"], "failed", args)
        if not isinstance(result, dict):
            # 契约内容无法解析：适配失败，回退通用详情并记诊断
            logger.warning("display 适配器无法解析工具 %s 的结果，回退通用详情", name)
            return None
        if result.get("approval") == "denied":
            # 审批拒绝优先于一切业务字段：操作未执行
            return _display(
                builder["kind"],
                "denied",
                args,
                message=str(result.get("message") or ""),
            )
        error = result.get("error")
        if isinstance(error, dict):
            return _display(
                builder["kind"],
                "failed",
                args,
                error_code=str(error.get("code") or ""),
                message=str(error.get("message") or ""),
            )
        fields = builder["fields"](args, result)
        if fields is None:
            return _display(builder["kind"], "unknown", args)
        return _display(builder["kind"], "succeeded", args, **fields)
    except Exception:  # noqa: BLE001 —— 展示失败绝不影响已完成业务操作
        logger.warning("display 适配器构建失败，回退通用详情", exc_info=True)
        return None


def _display(kind: str, outcome: str, args: dict, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"version": DISPLAY_VERSION, "kind": kind, "outcome": outcome}
    sku = args.get("sku")
    if isinstance(sku, str) and sku:
        out["sku"] = sku
    out.update({k: v for k, v in fields.items() if v is not None and v != ""})
    return out


def _stock_query_fields(args: dict, result: dict) -> dict[str, Any] | None:
    # demo check_stock 用 stock/available，MCP get_stock 用 quantity
    quantity = result.get("quantity", result.get("stock"))
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        return None
    return {
        "quantity": quantity,
        "available": result.get("available", quantity > 0),
    }


def _stock_adjustment_fields(args: dict, result: dict) -> dict[str, Any] | None:
    delta = args.get("delta")
    quantity_after = result.get("quantity")
    if not isinstance(delta, int) or isinstance(delta, bool):
        return None
    if not isinstance(quantity_after, int) or isinstance(quantity_after, bool):
        # 调整成功的返回必须带调整后的库存；缺失即无法确认
        return None
    return {"delta": delta, "quantity_after": quantity_after}


def _order_creation_fields(args: dict, result: dict) -> dict[str, Any] | None:
    order_id = result.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        return None
    total = result.get("total_amount")
    return {
        "order_id": order_id,
        "status": str(result.get("status") or ""),
        "customer": str(result.get("customer") or ""),
        "total_amount": total if isinstance(total, (int, float)) else None,
    }


_BUILDERS = {
    "check_stock": {"kind": "stock_query", "fields": _stock_query_fields},
    "get_stock": {"kind": "stock_query", "fields": _stock_query_fields},
    "adjust_stock": {"kind": "stock_adjustment", "fields": _stock_adjustment_fields},
    "create_order": {"kind": "order_creation", "fields": _order_creation_fields},
}
