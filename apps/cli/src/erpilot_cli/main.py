"""Erpilot CLI（typer + rich）：chat 流式对话 + replay trace 回放。

M3 第 2 周起 CLI 默认经 MCP 桥消费真数据（mcp_erp.server）；--tools demo
回退到 agent_core 内置假工具（离线演示 / 协议调试用）。CLI 属于组合层，
依赖业务包没有问题——agent_core 本体保持纯净。

用法：
    uv run erpilot chat "订单 123 里买了什么？还有货吗？有货的话报个价"
    uv run erpilot chat --tools demo                 # 假数据演示
    uv run erpilot replay traces/xxx.jsonl
"""

import asyncio
import os
from pathlib import Path

import typer
from agent_core.demo_tools import DEFAULT_PROMPT, DEMO_TOOLS, SYSTEM_PROMPT
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig, TextDelta
from agent_core.loop import AgentLoop, LoopEnd, StepStarted, ToolCallFinished, ToolCallStarted
from agent_core.prices import cost_of
from agent_core.tools import Tool
from agent_core.trace import JsonlTraceRecorder, format_transcript, load_records, new_trace_path
from mcp_erp import build_agent_tools
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Erpilot 掌柜助手", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

_TOOL_CONTENT_LIMIT = 300  # 终端里工具结果只展示片段，全文在 trace 里
_DEFAULT_TRACE_DIR = Path("traces")


def _resolve_tools(mode: str) -> list[Tool]:
    """mcp：经 MCP 桥取真数据工具（需先 seed）；demo：内置假工具。"""
    if mode == "demo":
        return list(DEMO_TOOLS)
    try:
        return build_agent_tools()
    except FileNotFoundError as exc:
        err_console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from None


def _trace_sinks() -> list:
    """Langfuse 双写（ADR-0003 收尾）：keys 齐全才启用，失败只告警不阻断。"""
    if not (
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    ):
        return []
    try:
        from agent_core.observability import LangfuseTraceSink

        return [LangfuseTraceSink()]
    except Exception as exc:
        err_console.print(f"[yellow]Langfuse 双写未启用（本地 trace 照常）：{exc}[/yellow]")
        return []


@app.command()
def chat(
    # typer 的参数声明必须就地调用 Argument/Option（B008 豁免）
    prompt: list[str] = typer.Argument(None, help="提示词；缺省用默认演示任务"),  # noqa: B008
    trace_dir: Path = typer.Option(_DEFAULT_TRACE_DIR, "--trace-dir", help="trace 目录"),  # noqa: B008
    tools_mode: str = typer.Option(
        "mcp", "--tools", help="工具来源：mcp（真数据，默认）或 demo（内置假数据）"
    ),  # noqa: B008
) -> None:
    """流式跑一轮 agent 对话：rich 渲染 + trace 落盘 JSONL。"""
    text = " ".join(prompt) if prompt else DEFAULT_PROMPT
    try:
        config = LLMConfig.from_env()
    except RuntimeError as exc:
        err_console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from None

    tools = _resolve_tools(tools_mode)
    agent = AgentLoop(LLMClient(config), tools=tools)
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    trace_path = new_trace_path(trace_dir, config.model)
    sinks = _trace_sinks()
    recorder = JsonlTraceRecorder(trace_path, config.model, sinks=sinks)

    console.print(
        f"[dim]模型 {config.model} · 工具 {'MCP 真数据' if tools_mode == 'mcp' else 'demo'}"
        f" × {len(tools)} · trace {trace_path}"
        + (" · Langfuse 双写" if sinks else "")
        + "[/dim]\n"
    )
    console.print(text, style="bold cyan", markup=False, soft_wrap=True)
    console.print()
    end = asyncio.run(_stream_chat(agent, messages, recorder))
    if end is None:  # pragma: no cover —— run() 保证产出 LoopEnd
        return
    _print_summary(config.model, end)


async def _stream_chat(
    agent: AgentLoop, messages: list, recorder: JsonlTraceRecorder
) -> LoopEnd | None:
    end: LoopEnd | None = None
    async for event in recorder.run(agent, messages):
        match event:
            case TextDelta(text=delta):
                console.print(delta, end="", markup=False, soft_wrap=True)
            case StepStarted(step=s) if s > 1:
                console.print()  # 新一轮生成另起一行
            case ToolCallStarted(call=call):
                console.print(f"\n[dim]▶ {call.name}({call.arguments})[/dim]")
            case ToolCallFinished(name=name, content=content, ok=ok):
                mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
                brief = (
                    content
                    if len(content) <= _TOOL_CONTENT_LIMIT
                    else content[:_TOOL_CONTENT_LIMIT] + "…"
                )
                console.print(f"{mark} [dim]{name} → {brief}[/dim]")
            case LoopEnd() as reached:
                end = reached
    console.print("\n")
    return end


def _print_summary(model: str, end: LoopEnd) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row(
        "结束",
        f"{end.steps} 步 · "
        + ("正常" if end.completed else "[yellow]触发 max_steps 防护[/yellow]"),
    )
    if end.usage is not None:
        table.add_row(
            "tokens",
            f"输入 {end.usage.prompt_tokens} / 输出 {end.usage.completion_tokens}"
            f" / 共 {end.usage.total_tokens}",
        )
        cost = cost_of(model, end.usage)
        table.add_row("成本", f"≈ ¥{cost:.4f}" if cost is not None else "（模型未收录价目）")
    console.print(table)


@app.command()
def replay(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="trace JSONL 文件"),  # noqa: B008
) -> None:
    """把一次 trace 还原成可读对话：轮次、工具调用、token/成本/耗时。"""
    console.print(format_transcript(load_records(path)))


def main() -> None:
    if (dotenv := find_dotenv(Path(__file__).resolve())) is not None:
        load_dotenv(dotenv)
    app()


if __name__ == "__main__":
    main()
