"""工具协议单测：Pydantic 签名 → OpenAI tools schema 的转换。"""

from agent_core.tools import Tool, tool
from pydantic import BaseModel, Field


class OrderQuery(BaseModel):
    order_id: str = Field(description="订单号")
    verbose: bool = False


async def _get_order(params: OrderQuery) -> dict[str, str]:
    return {"status": "ok"}


def test_openai_schema_structure_and_no_titles() -> None:
    result = Tool(
        name="get_order_status",
        description="按订单号查询订单状态",
        params_model=OrderQuery,
        handler=_get_order,
    ).openai_schema()

    fn = result["function"]
    assert result["type"] == "function"
    assert fn["name"] == "get_order_status"
    assert fn["description"] == "按订单号查询订单状态"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"order_id"}
    assert params["properties"]["order_id"]["type"] == "string"
    assert params["properties"]["verbose"]["default"] is False


def test_decorator_wraps_handler_into_tool() -> None:
    wrapped = tool(name="get_order_status", description="按订单号查询订单状态", params=OrderQuery)(
        _get_order
    )

    assert isinstance(wrapped, Tool)
    assert wrapped.handler is _get_order
    assert wrapped.params_model is OrderQuery
