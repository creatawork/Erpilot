"""M1 验收入口：
- 第 1 周：流式对话 + token/成本
- 第 2 周：工具调用 loop——"查订单 123 的状态"
- 第 3 周：多步任务——订单 → 库存 → 报价 → 给顾客回复建议（含并行工具调用）
- 第 4 周：rich 版 CLI 见 `erpilot chat`（apps/cli），本入口保留为最简演示

用法：
    uv run --package agent-core python -m agent_core "订单 123 里的商品还有货吗？有货的话报个价"
    uv run --package agent-core python -m agent_core   # 不带参数用默认提示词
"""

import asyncio
import sys
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from agent_core.demo_tools import DEFAULT_PROMPT, DEMO_TOOLS, SYSTEM_PROMPT
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.events import LoopEnd, ToolCallFinished, ToolCallStarted
from agent_core.graph_runtime import LangGraphRuntime
from agent_core.llm import LLMClient, LLMConfig, TextDelta
from agent_core.prices import cost_of


async def _demo(config: LLMConfig, prompt: str) -> None:
    agent = LangGraphRuntime(LLMClient(config), tools=DEMO_TOOLS, checkpointer=InMemorySaver())
    print(f"模型：{config.model}")
    print("---")
    end: LoopEnd | None = None
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
    if (dotenv := find_dotenv(Path(__file__).resolve())) is not None:
        load_dotenv(dotenv)
    prompt = " ".join(sys.argv[1:]) or DEFAULT_PROMPT
    try:
        config = LLMConfig.from_env()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_demo(config, prompt))


if __name__ == "__main__":
    main()
