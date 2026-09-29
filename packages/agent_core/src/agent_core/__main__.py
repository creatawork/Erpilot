"""M1 第 1 周验收入口：agent_core 能流式对话并打印 token / 成本。

用法：
    uv run --package agent-core python -m agent_core "帮我写一句店铺招牌文案"
    uv run --package agent-core python -m agent_core   # 不带参数用默认提示词
"""

import asyncio
import os
import sys
from pathlib import Path

from agent_core.llm import LLMClient, LLMConfig, StreamEnd, TextDelta
from agent_core.prices import cost_of

DEFAULT_PROMPT = "用一句话介绍你自己。"


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
    client = LLMClient(config)
    print(f"模型：{config.model}")
    print("---")
    usage = None
    async for event in client.stream_chat([{"role": "user", "content": prompt}]):
        match event:
            case TextDelta(text=text):
                print(text, end="", flush=True)
            case StreamEnd(usage=final_usage):
                usage = final_usage
    print()
    print("---")
    if usage is None:
        print("（端点未返回 usage，无法统计 token）")
        return
    print(
        f"tokens：输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens}"
        f" / 共 {usage.total_tokens}"
    )
    cost = cost_of(config.model, usage)
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
