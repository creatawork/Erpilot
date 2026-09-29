"""M1 验收入口：
- 第 1 周：流式对话 + token/成本
- 第 2 周：工具调用 loop——内置一个假订单查询工具，演示"查订单 123 的状态"

用法：
    uv run --package agent-core python -m agent_core "查一下订单 123 的状态"
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

DEFAULT_PROMPT = "查一下订单 123 的状态"

# ---- 演示用假订单表：M3 起真实 ERP 工具由 mcp_erp（FastMCP）提供，这里是协议演示 ----


class OrderStatusQuery(BaseModel):
    order_id: str = Field(description="订单号，例如 123")


_FAKE_ORDERS = {
    "123": {"status": "已发货", "carrier": "顺丰", "eta": "明天下午"},
    "456": {"status": "待付款"},
}


@tool(name="get_order_status", description="按订单号查询订单的当前状态", params=OrderStatusQuery)
async def get_order_status(params: OrderStatusQuery) -> dict[str, str]:
    return _FAKE_ORDERS.get(params.order_id) or {"error": f"订单 {params.order_id} 不存在"}


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
    agent = AgentLoop(LLMClient(config), tools=[get_order_status])
    print(f"模型：{config.model}")
    print("---")
    end: LoopEnd | None = None
    async for event in agent.run([{"role": "user", "content": prompt}]):
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
