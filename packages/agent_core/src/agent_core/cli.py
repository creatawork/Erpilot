"""M1 第 4 周产出：typer + rich CLI——chat 流式对话（trace 自动落盘）+ replay 回放。

用法：
    uv run erpilot chat "订单 123 里的商品还有货吗？有货的话报个价"
    uv run erpilot chat                 # 不带参数用默认演示任务
    uv run erpilot replay traces/xxx.jsonl
"""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agent_core.demo_tools import DEFAULT_PROMPT, DEMO_TOOLS, SYSTEM_PROMPT
from agent_core.dotenv import find_dotenv, load_dotenv
from agent_core.llm import LLMClient, LLMConfig, TextDelta
from agent_core.loop import AgentLoop, LoopEnd, StepStarted, ToolCallFinished, ToolCallStarted
from agent_core.prices import cost_of
from agent_core.trace import JsonlTraceRecorder, format_transcript, load_records, new_trace_path

app = typer.Typer(help="Erpilot 掌柜助手（M1：手写 agent loop 演示）", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

_TOOL_CONTENT_LIMIT = 300  # 终端里工具结果只展示片段，全文在 trace 里
_DEFAULT_TRACE_DIR = Path("traces")


@app.command()
def chat(
    # typer 的参数声明必须就地调用 Argument/Option（B008 豁免）
    prompt: list[str] = typer.Argument(None, help="提示词；缺省用默认演示任务"),  # noqa: B008
    trace_dir: Path = typer.Option(_DEFAULT_TRACE_DIR, "--trace-dir", help="trace 目录"),  # noqa: B008
) -> None:
    """流式跑一轮 agent 对话：rich 渲染 + trace 落盘 JSONL。"""
    text = " ".join(prompt) if prompt else DEFAULT_PROMPT
    try:
        config = LLMConfig.from_env()
    except RuntimeError as exc:
        err_console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from None

    agent = AgentLoop(LLMClient(config), tools=DEMO_TOOLS)
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    trace_path = new_trace_path(trace_dir, config.model)
    recorder = JsonlTraceRecorder(trace_path, config.model)

    console.print(f"[dim]模型 {config.model} · trace {trace_path}[/dim]\n")
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
