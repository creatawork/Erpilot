"""M1 验收入口：
- 第 1 周：流式对话 + token/成本
- 第 2 周：工具调用 loop——"查订单 123 的状态"
- 第 3 周：多步任务——订单 → 库存 → 报价 → 给顾客回复建议（含并行工具调用）

用法：
    uv run --package agent-core python -m agent_core "订单 123 里的商品还有货吗？有货的话报个价"
    uv run --package agent-core python -m agent_core   # 不带参数用默认提示词
"""

import asyncio
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from agent_core.llm import LLMClient, LLMConfig, TextDelta
from agent_core.loop import AgentLoop, LoopEnd, ToolCallFinished, ToolCallStarted
from agent_core.prices import cost_of
from agent_core.tools import tool

DEFAULT_PROMPT = (
    "订单 123 里买了什么？这些商品现在还有货吗？"
    "有货的话报个价，最后给我一句能直接发给顾客的话。"
)

# ---- 演示用假数据：M3 起真实 ERP 工具由 mcp_erp（FastMCP）提供，这里是协议演示 ----


class OrderStatusQuery(BaseModel):
    order_id: str = Field(description="订单号，例如 123")


class SkuQuery(BaseModel):
    sku: str = Field(description="商品 SKU 编码，例如 A1001")


_FAKE_ORDERS = {
    "123": {
        "status": "待发货",
        "carrier": None,
        "items": [{"sku": "A1001", "name": "景德镇青瓷茶具", "qty": 2}],
    },
    "456": {
        "status": "待付款",
        "carrier": None,
        "items": [{"sku": "B2002", "name": "加厚宣纸 100 张", "qty": 5}],
    },
}
_FAKE_STOCK = {"A1001": 18, "B2002": 0}
_FAKE_PRICES = {"A1001": 299.0, "B2002": 45.5}


@tool(
    name="get_order_status", description="按订单号查询订单状态与所含商品", params=OrderStatusQuery
)
async def get_order_status(params: OrderStatusQuery) -> dict[str, object]:
    return _FAKE_ORDERS.get(params.order_id) or {"error": f"订单 {params.order_id} 不存在"}


@tool(name="check_stock", description="按 SKU 查询商品当前库存", params=SkuQuery)
async def check_stock(params: SkuQuery) -> dict[str, object]:
    stock = _FAKE_STOCK.get(params.sku)
    if stock is None:
        return {"error": f"SKU {params.sku} 不存在"}
    return {"sku": params.sku, "stock": stock, "available": stock > 0}


@tool(name="get_price", description="按 SKU 查询商品当前售价（元）", params=SkuQuery)
async def get_price(params: SkuQuery) -> dict[str, object]:
    price = _FAKE_PRICES.get(params.sku)
    if price is None:
        return {"error": f"SKU {params.sku} 不存在"}
    return {"sku": params.sku, "price": price, "currency": "CNY"}


def _load_dotenv(path: Path) -> None:
    """把 .env 的 KEY=VALUE 注入环境变量；已存在的变量不覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _find_dotenv() -> Path | None:
    """从本文件向上逐级找 .env（仓库根目录）。"""
    parents = Path(__file__).resolve().parents
    return next((p / ".env" for p in parents if (p / ".env").is_file()), None)


async def _demo(config: LLMConfig, prompt: str) -> None:
    agent = AgentLoop(LLMClient(config), tools=[get_order_status, check_stock, get_price])
    print(f"模型：{config.model}")
    print("---")
    end: LoopEnd | None = None
    messages: list = [
        {
            "role": "system",
            "content": "你是 Erpilot 掌柜助手：查订单、盘库存、算报价。"
            "需要数据时必须调用工具查询，不要编造。",
        },
        {"role": "user", "content": prompt},
    ]
    async for event in agent.run(messages):
        match event:
            case TextDelta(text=text):
                print(text, end="", flush=True)
            case ToolCallStarted(call=call):
                print(f"\n[工具] {call.name}({call.arguments})")
            case ToolCallFinished(name=name, content=content, ok=ok):
                print(f"[{'结果' if ok else '错误'}] {name} → {content}")
            case LoopEnd() as reached:
                end = reached
    print()
    print("---")
    if end is None:  # pragma: no cover —— run() 保证产出 LoopEnd
        return
    print(f"步数：{end.steps}（{'正常结束' if end.completed else '触发 max_steps 防护'}）")
    if end.usage is None:
        print("（端点未返回 usage，无法统计 token）")
        return
    print(
        f"tokens：输入 {end.usage.prompt_tokens} / 输出 {end.usage.completion_tokens}"
        f" / 共 {end.usage.total_tokens}"
    )
    cost = cost_of(config.model, end.usage)
    print(f"成本：≈ ¥{cost:.4f}" if cost is not None else "成本：（模型未收录价目，无法估算）")


def main() -> None:
    if (dotenv := _find_dotenv()) is not None:
        _load_dotenv(dotenv)
    prompt = " ".join(sys.argv[1:]) or DEFAULT_PROMPT
    try:
        config = LLMConfig.from_env()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_demo(config, prompt))


if __name__ == "__main__":
    main()
