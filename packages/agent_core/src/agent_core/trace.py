"""M1 第 4 周产出：本地 trace——JSONL 事件溯源落盘 + 回放（计划 §6 第 4 周）。

为什么先 JSONL 落盘而不是直接上 Langfuse，见 ADR-0003：一行一个 JSON 事件、
追加写并逐行 flush——进程崩了已写的行还在，tail -f 能盯，grep/jq 能查；
格式自有可控，接 Langfuse / OTel GenAI 语义约定时把本模块换成一个 sink 即可。

记录模型 v1（run_id 关联一次 agent.run，六类记录按时间顺序成流水）：

- run_start ：model / ts / messages —— 开跑时的历史快照（提问前的完整上下文）
- step_start：step / ts —— 一轮 LLM 请求开始
- tool_call ：step / id / name / arguments / content / ok / duration_ms —— 每次工具调用一行
- step_end  ：step / text / usage / cost / duration_ms —— 该轮模型文本输出与计量
- run_end   ：steps / completed / usage / cost / duration_ms / messages —— 结束时完整历史
- run_error ：error / duration_ms —— 异常留痕后原样抛出，trace 不吞错

用法（事件原样透传，不影响既有消费方）：

    recorder = JsonlTraceRecorder(path, model=config.model)
    async for event in recorder.run(agent, messages):
        ...

回放：`erpilot replay traces/xxx.jsonl`（cli.py），或直接读本模块的
load_records / format_transcript。M1 验收线"一次完整任务的 trace 可回放，
成本/延迟有数字"即由此承担。
"""

import copy
import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam

from agent_core.llm import TextDelta, ToolCall, Usage
from agent_core.loop import (
    AgentEvent,
    AgentLoop,
    LoopEnd,
    StepEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from agent_core.prices import cost_of

TRACE_VERSION = 1


def _now() -> str:
    """本地时区 ISO 8601（毫秒精度）——本地排障场景比 UTC 直观。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _usage_dict(usage: Usage | None) -> dict | None:
    return asdict(usage) if usage is not None else None


class JsonlTraceRecorder:
    """把一次 agent.run 的事件流写成 JSONL；对事件只是旁观，不做任何改写。"""

    def __init__(self, path: Path, model: str) -> None:
        self._path = path
        self._model = model

    async def run(
        self, agent: AgentLoop, messages: list[ChatCompletionMessageParam]
    ) -> AsyncIterator[AgentEvent]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        run_id = uuid4().hex[:12]
        started = time.perf_counter()
        with self._path.open("a", encoding="utf-8") as f:

            def write(record: dict) -> None:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                f.flush()

            write({
                "type": "run_start",
                "v": TRACE_VERSION,
                "run_id": run_id,
                "model": self._model,
                "ts": _now(),
                "messages": copy.deepcopy(messages),
            })
            step = 0
            step_text: list[str] = []
            pending: dict[str, tuple[float, ToolCall]] = {}
            try:
                async for event in agent.run(messages):
                    match event:
                        case StepStarted(step=s):
                            step = s
                            step_text.clear()
                            write({
                                "type": "step_start", "run_id": run_id, "step": s, "ts": _now()
                            })
                        case TextDelta(text=text):
                            step_text.append(text)
                        case StepEnd(step=s, usage=usage, duration_ms=ms):
                            write({
                                "type": "step_end",
                                "run_id": run_id,
                                "step": s,
                                "ts": _now(),
                                "text": "".join(step_text),
                                "usage": _usage_dict(usage),
                                "cost": cost_of(self._model, usage),
                                "duration_ms": ms,
                            })
                        case ToolCallStarted(call=call):
                            pending[call.id] = (time.perf_counter(), call)
                        case ToolCallFinished(call_id=cid, name=name, content=content, ok=ok):
                            t0, call = pending.pop(cid, (time.perf_counter(), None))
                            write({
                                "type": "tool_call",
                                "run_id": run_id,
                                "step": step,
                                "ts": _now(),
                                "id": cid,
                                "name": name,
                                "arguments": call.arguments if call else "",
                                "content": content,
                                "ok": ok,
                                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                            })
                        case LoopEnd(steps=steps, usage=usage, completed=completed):
                            write({
                                "type": "run_end",
                                "run_id": run_id,
                                "ts": _now(),
                                "steps": steps,
                                "completed": completed,
                                "usage": _usage_dict(usage),
                                "cost": cost_of(self._model, usage),
                                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                                "messages": copy.deepcopy(messages),
                            })
                    yield event
            except Exception as exc:
                write({
                    "type": "run_error",
                    "run_id": run_id,
                    "ts": _now(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                })
                raise


def new_trace_path(trace_dir: Path, model: str) -> Path:
    """按时间 + 随机后缀生成 trace 文件名：traces/20260930-141530-3fa2b1c0.jsonl。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return trace_dir / f"{stamp}-{uuid4().hex[:8]}.jsonl"


def load_records(path: Path) -> list[dict]:
    """读回一个 trace 文件（每行一个 JSON 记录），坏行报错不静默。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def format_transcript(records: Sequence[dict], *, max_chars: int = 600) -> str:
    """把记录流水还原成可读对话文本——"trace 可回放"的默认形态。"""
    lines: list[str] = []
    for r in records:
        kind = r.get("type")
        if kind == "run_start":
            lines.append(f"== run {r['run_id']} · {r['model']} · {r['ts']} ==")
            for m in r.get("messages", []):
                if m.get("role") == "user":
                    lines.append(f"[用户] {_shorten(m['content'], max_chars)}")
        elif kind == "step_start":
            lines.append(f"--- 第 {r['step']} 轮 ---")
        elif kind == "step_end":
            usage = r.get("usage") or {}
            tokens = f"in {usage.get('prompt_tokens', 0)} / out {usage.get('completion_tokens', 0)}"
            cost = r.get("cost")
            lines.append(
                f"（{r['duration_ms']:.0f}ms · {tokens} tok"
                + (f" · ≈¥{cost:.4f}" if cost is not None else "")
                + "）"
            )
            if r.get("text"):
                lines.append(f"[模型] {_shorten(r['text'], max_chars)}")
        elif kind == "tool_call":
            mark = "✓" if r.get("ok") else "✗"
            lines.append(
                f"[工具{mark}] {r['name']}({_shorten(r.get('arguments', ''), 200)})"
                f" → {_shorten(r.get('content', ''), max_chars)}（{r['duration_ms']:.0f}ms）"
            )
        elif kind == "run_end":
            usage = r.get("usage") or {}
            cost = r.get("cost")
            lines.append(
                f"== 结束：{r['steps']} 步 · 共 {usage.get('total_tokens', 0)} tok"
                + (f" · ≈¥{cost:.4f}" if cost is not None else "")
                + f" · {r['duration_ms']:.0f}ms =="
            )
        elif kind == "run_error":
            lines.append(f"!! 异常：{r['error']}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """run_end 记录的摘要（CLI/API 结束面板用）。"""

    steps: int
    completed: bool
    usage: Usage | None
    cost: float | None
    duration_ms: float


def summarize(records: Sequence[dict]) -> TraceSummary | None:
    """取 trace 的 run_end 摘要；没有（中途异常）返回 None。"""
    for r in reversed(records):
        if r.get("type") == "run_end":
            usage = r.get("usage")
            return TraceSummary(
                steps=r["steps"],
                completed=r["completed"],
                usage=Usage(**usage) if usage else None,
                cost=r.get("cost"),
                duration_ms=r["duration_ms"],
            )
    return None
