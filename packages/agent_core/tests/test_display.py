"""展示适配器单测（设计 5.2）：成功、业务 error、拒绝、未知格式；数据以工具契约为准。"""

import json

from agent_core.display import build_display


def test_check_stock_success_maps_to_stock_query() -> None:
    display = build_display(
        "check_stock",
        {"sku": "A1001"},
        json.dumps({"sku": "A1001", "stock": 18, "available": True}),
        ok=True,
    )
    assert display == {
        "version": 1,
        "kind": "stock_query",
        "outcome": "succeeded",
        "sku": "A1001",
        "quantity": 18,
        "available": True,
    }


def test_mcp_get_stock_shape_uses_quantity() -> None:
    display = build_display(
        "get_stock",
        {"sku": "A1001"},
        json.dumps({"sku": "A1001", "quantity": 7, "warehouse": "主仓"}),
        ok=True,
    )
    assert display is not None
    assert display["kind"] == "stock_query" and display["outcome"] == "succeeded"
    assert display["quantity"] == 7


def test_adjust_stock_success_takes_delta_from_verified_arguments() -> None:
    """调整量取自已验证的执行参数，调整后库存取自工具结果。"""
    display = build_display(
        "adjust_stock",
        {"sku": "A1001", "delta": 9},
        json.dumps({"sku": "A1001", "quantity": 27, "note": "库存已调整（+9）"}),
        ok=True,
    )
    assert display == {
        "version": 1,
        "kind": "stock_adjustment",
        "outcome": "succeeded",
        "sku": "A1001",
        "delta": 9,
        "quantity_after": 27,
    }


def test_create_order_success_keeps_order_fields() -> None:
    display = build_display(
        "create_order",
        {"customer": "王掌柜", "items": [{"sku": "A1001", "quantity": 2}]},
        json.dumps(
            {
                "order_id": "SO20261007-0001",
                "customer": "王掌柜",
                "status": "待付款",
                "items": [],
                "total_amount": 598.0,
            }
        ),
        ok=True,
    )
    assert display == {
        "version": 1,
        "kind": "order_creation",
        "outcome": "succeeded",
        "customer": "王掌柜",
        "order_id": "SO20261007-0001",
        "status": "待付款",
        "total_amount": 598.0,
    }


def test_business_error_is_failed_despite_transport_ok() -> None:
    """transport ok、业务 error 字段共同决定结果：不显示绿色成功卡。"""
    display = build_display(
        "adjust_stock",
        {"sku": "X", "delta": 1},
        json.dumps({"error": {"code": "not_found", "message": "商品不存在", "hint": ""}}),
        ok=True,
    )
    assert display is not None
    assert display["outcome"] == "failed"
    assert display["error_code"] == "not_found"


def test_denial_takes_priority_over_ok_field() -> None:
    """审批拒绝以正常 JSON 返回（ok=True），必须分类为 denied。"""
    display = build_display(
        "adjust_stock",
        {"sku": "A1001", "delta": 9},
        json.dumps({"approval": "denied", "message": "操作未执行：额度不足"}),
        ok=True,
    )
    assert display is not None
    assert display["outcome"] == "denied"
    assert display["message"] == "操作未执行：额度不足"


def test_transport_failure_is_failed() -> None:
    display = build_display("adjust_stock", {"sku": "A1001", "delta": 1}, "", ok=False)
    assert display is not None
    assert display["outcome"] == "failed"


def test_unrecognized_success_shape_is_unknown() -> None:
    display = build_display(
        "adjust_stock", {"sku": "A1001", "delta": 1}, json.dumps({"weird": 1}), ok=True
    )
    assert display is not None
    assert display["outcome"] == "unknown"


def test_unknown_tool_and_broken_content_fall_back_to_none() -> None:
    assert build_display("mystery_tool", {}, "{}", ok=True) is None
    assert build_display("check_stock", {"sku": "A"}, "not json", ok=True) is None
    assert build_display("check_stock", "not json", "{}", ok=True) is None


def test_scoped_stock_result_uses_committed_sku_and_delta():
    display = build_display(
        "adjust_stock",
        {"operation_id": "opaque"},
        json.dumps({"sku": "A", "delta": -3, "quantity": 97}),
        ok=True,
    )
    assert display == dict(
        version=1,
        kind="stock_adjustment",
        outcome="succeeded",
        sku="A",
        delta=-3,
        quantity_after=97,
    )


def test_cancel_result_is_business_success_and_errors_are_not_success():
    result = {
        "order_id": "SO1",
        "customer": "客户",
        "status": "已取消",
        "total_amount": 980,
        "result_note": "存在库存记录的商品已回补，此操作不执行退款",
    }
    display = build_display("cancel_order", {"operation_id": "opaque"}, json.dumps(result), ok=True)
    assert display["kind"] == "order_cancellation" and display["outcome"] == "succeeded"
    assert display["order_id"] == "SO1" and display["total_amount"] == 980
    assert display["message"] == "存在库存记录的商品已回补，此操作不执行退款"
    failed = build_display(
        "cancel_order",
        {},
        json.dumps(
            {
                "error": {
                    "code": "operation_changed",
                    "message": "库存已变化",
                    "hint": "请重新预览并审批",
                }
            }
        ),
        ok=True,
    )
    assert failed["outcome"] == "failed" and failed["message"] == "库存已变化"
    assert failed["hint"] == "请重新预览并审批"
