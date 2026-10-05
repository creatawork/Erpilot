"""Langfuse 远程 sink——可观测双写的远程侧（ADR-0003 收尾，M3 第 4 周接入）。

分工（ADR-0003 的既定路径）：
- 本地 JSONL（trace.py）是兜底 sink：永远写、先于远程写，观测管道自身故障
  时不留真空；
- 本模块把 trace 记录映射到 Langfuse（自托管，OTel 语义约定的宿主）：
  run → trace，step → generation，tool_call → span，run_error → level=ERROR。

Langfuse SDK 是可选依赖（`uv sync --package agent-core --extra langfuse`），
且只在 keys 齐全时才构造——没配 Langfuse 的环境零成本。write 吞掉自己的
异常（打印告警后继续）：远程上报失败不能反噬被观测的业务链路。
"""

import os
import sys
from typing import Any

_INSTALL_HINT = (
    "Langfuse 已配置但 SDK 未安装：uv sync --package agent-core --extra langfuse"
)


class LangfuseTraceSink:
    """把 JsonlTraceRecorder 的记录流转发给 Langfuse。

    client 参数注入 duck-typed 客户端（单测用 fake）；缺省时懒加载真实 SDK，
    读 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 环境变量。
    """

    def __init__(self, client: Any = None) -> None:
        if client is None:
            try:
                from langfuse import Langfuse
            except ImportError as exc:  # pragma: no cover —— 取决于安装环境
                raise RuntimeError(_INSTALL_HINT) from exc
            client = Langfuse()
        self._client = client
        self._trace: Any = None
        self._step_span: Any = None
        self._model: str | None = None

    def write(self, record: dict) -> None:
        try:
            self._dispatch(record)
        except Exception as exc:  # 上报失败不反噬业务（docstring 约定）
            print(f"[langfuse-sink] 上报失败（忽略）: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    def close(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:  # pragma: no cover
            print(f"[langfuse-sink] flush 失败（忽略）: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    # ---- 记录 → Langfuse 对象的映射 ----

    def _dispatch(self, record: dict) -> None:
        kind = record.get("type")
        if kind == "run_start":
            self._model = record.get("model")
            self._trace = self._client.trace(
                name="erpilot-agent-run",
                session_id=record["run_id"],
                input=record.get("messages"),
                metadata={"model": record.get("model")},
            )
        elif kind == "step_start" and self._trace is not None:
            self._step_span = self._trace.span(name=f"step-{record['step']}")
        elif kind == "tool_call":
            parent = self._step_span or self._trace
            if parent is None:
                return
            span = parent.span(
                name=f"tool:{record.get('name')}",
                input=record.get("arguments"),
                output=record.get("content"),
                metadata={
                    "ok": record.get("ok"),
                    "duration_ms": record.get("duration_ms"),
                },
            )
            span.end()
        elif kind == "step_end" and self._trace is not None:
            usage = record.get("usage") or {}
            gen = self._trace.generation(
                name=f"step-{record['step']}",
                model=self._model,
                output=record.get("text"),
                usage={
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                },
                metadata={
                    "cost": record.get("cost"),
                    "duration_ms": record.get("duration_ms"),
                },
            )
            gen.end()
            self._end_step_span()
        elif kind == "run_end" and self._trace is not None:
            output = next(
                (
                    m.get("content")
                    for m in reversed(record.get("messages") or [])
                    if m.get("role") == "assistant"
                ),
                None,
            )
            self._trace.update(
                output=output,
                metadata={
                    "steps": record.get("steps"),
                    "completed": record.get("completed"),
                    "cost": record.get("cost"),
                    "duration_ms": record.get("duration_ms"),
                },
            )
            self._end_step_span()
            self._trace = None
        elif kind == "run_error" and self._trace is not None:
            self._trace.update(level="ERROR", status_message=record.get("error"))
            self._end_step_span()
            self._trace = None

    def _end_step_span(self) -> None:
        if self._step_span is not None:
            self._step_span.end()
            self._step_span = None


def langfuse_sink_from_env() -> LangfuseTraceSink | None:
    """keys 齐全才启用双写；否则返回 None（本地 JSONL 独挑，零成本路径）。"""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    return LangfuseTraceSink()
