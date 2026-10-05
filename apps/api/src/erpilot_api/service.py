"""会话管理 + agent 宿主：每个会话一份消息历史，每次 run 顺带落一份 trace。

工具集由组合层注入（评审遗留项，M3 第 2 周落地）——api 本体不关心工具来自
MCP 桥还是演示假数据。M1 第 4 周的最小实现：单进程内存会话（重启即失）、
trace 本地 JSONL。持久化（Postgres checkpointer）与远程可观测（Langfuse）
按路线图在 M6 前后接入，届时替换的是本模块的存储层，事件协议与前端不动。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from agent_core.approval import ApprovalDecision
from agent_core.demo_tools import SYSTEM_PROMPT
from agent_core.llm import LLMClient
from agent_core.loop import AgentEvent, AgentLoop, LoopConfig
from agent_core.tools import Tool
from agent_core.trace import JsonlTraceRecorder, TraceSink, new_trace_path


def _sinks_from_env() -> list[TraceSink]:
    """Langfuse keys 齐全 → 双写远程 sink；否则本地 JSONL 独挑（ADR-0003）。"""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from agent_core.observability import LangfuseTraceSink

        return [LangfuseTraceSink()]
    except Exception as exc:  # SDK 未装 / 配置错：本地兜底仍在，服务不因此起不来
        print(f"[trace] Langfuse 双写未启用：{exc}")
        return []


class ChatService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], LLMClient],
        model: str,
        trace_dir: Path,
        tools: Sequence[Tool],
        loop_config: LoopConfig | None = None,
        approval_gate: Any | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._model = model
        self._trace_dir = trace_dir
        self._tools = list(tools)
        self._loop_config = loop_config or LoopConfig()
        self._approval_gate = approval_gate
        self._sinks = _sinks_from_env()
        self._client: LLMClient | None = None
        self._sessions: dict[str, list] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_trace: Path | None = None  # 最近一次 run 的 trace 文件（done 事件带回前端）

    @property
    def model(self) -> str:
        return self._model

    def respond_approval(self, pending_id: str, decision: ApprovalDecision) -> bool:
        """回填一次审批决策；无门或 pending_id 无效返回 False（不抛异常）。"""
        if self._approval_gate is None:
            return False
        return self._approval_gate.respond(pending_id, decision)

    def lock(self, session_id: str) -> asyncio.Lock:
        """每会话一把锁：同一会话的多次 run 串行，历史才不会交错。"""
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _session_messages(self, session_id: str) -> list:
        if session_id not in self._sessions:
            self._sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        return self._sessions[session_id]

    def start_run(
        self, session_id: str, message: str
    ) -> tuple[AgentLoop, list, JsonlTraceRecorder]:
        """把用户消息追加进会话历史，建好本轮的 agent 与 trace recorder。"""
        if self._client is None:
            self._client = self._client_factory()
        messages = self._session_messages(session_id)
        messages.append({"role": "user", "content": message})
        agent = AgentLoop(self._client, tools=self._tools, config=self._loop_config)
        trace_path = new_trace_path(self._trace_dir, self._model)
        self.last_trace = trace_path
        recorder = JsonlTraceRecorder(trace_path, self._model, sinks=self._sinks)
        return agent, messages, recorder

    async def stream_run(self, session_id: str, message: str) -> AsyncIterator[AgentEvent]:
        """带会话锁地跑一轮：调用方直接迭代事件，串行与历史维护都在这层。"""
        async with self.lock(session_id):
            agent, messages, recorder = self.start_run(session_id, message)
            async for event in recorder.run(agent, messages):
                yield event
