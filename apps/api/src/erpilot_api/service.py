"""会话管理 + agent 宿主：每个会话一份消息历史，每次 run 顺带落一份 trace。

M1 第 4 周的最小实现：单进程内存会话（重启即失）、trace 本地 JSONL。
持久化（Postgres checkpointer）与远程可观测（Langfuse）按路线图在 M6 前后接入，
届时替换的是本模块的存储层，事件协议与前端不动。
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from agent_core.demo_tools import DEMO_TOOLS, SYSTEM_PROMPT
from agent_core.llm import LLMClient
from agent_core.loop import AgentEvent, AgentLoop, LoopConfig
from agent_core.trace import JsonlTraceRecorder, new_trace_path


class ChatService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], LLMClient],
        model: str,
        trace_dir: Path,
        loop_config: LoopConfig | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._model = model
        self._trace_dir = trace_dir
        self._loop_config = loop_config or LoopConfig()
        self._client: LLMClient | None = None
        self._sessions: dict[str, list] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_trace: Path | None = None  # 最近一次 run 的 trace 文件（done 事件带回前端）

    @property
    def model(self) -> str:
        return self._model

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
        agent = AgentLoop(self._client, tools=DEMO_TOOLS, config=self._loop_config)
        trace_path = new_trace_path(self._trace_dir, self._model)
        self.last_trace = trace_path
        recorder = JsonlTraceRecorder(trace_path, self._model)
        return agent, messages, recorder

    async def stream_run(
        self, session_id: str, message: str
    ) -> AsyncIterator[AgentEvent]:
        """带会话锁地跑一轮：调用方直接迭代事件，串行与历史维护都在这层。"""
        async with self.lock(session_id):
            agent, messages, recorder = self.start_run(session_id, message)
            async for event in recorder.run(agent, messages):
                yield event
