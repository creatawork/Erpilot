"""Single-process session host; graph checkpoints own execution and history."""

import asyncio
import copy
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from pathlib import Path

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import system_prompt
from agent_core.events import AgentEvent, ApprovalPending, LoopEnd
from agent_core.graph_runtime import LangGraphRuntime
from agent_core.llm import LLMClient
from agent_core.runtime_config import LoopConfig
from agent_core.tools import Tool
from agent_core.trace import JsonlTraceRecorder, TraceSink, new_trace_path

from erpilot_api.checkpoint import thread_id_for_session
from erpilot_api.run_store import RunStore


class SessionError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _sinks_from_env() -> list[TraceSink]:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from agent_core.observability import LangfuseTraceSink

        return [LangfuseTraceSink()]
    except Exception as exc:
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
        approval_gate: StreamApprovalGate | None = None,
        run_store: RunStore | None = None,
        checkpointer=None,
    ):
        self._client_factory = client_factory
        self._model, self._trace_dir = model, trace_dir
        self._tools, self._loop_config = list(tools), loop_config or LoopConfig()
        self._approval_gate = approval_gate
        if any(t.risk is not None for t in tools) and approval_gate is None:
            raise ValueError("write tools require approval runtime")
        self._run_store, self._checkpointer = run_store, checkpointer
        self._sinks = _sinks_from_env()
        self._client = None
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_trace: Path | None = None

    @property
    def model(self):
        return self._model

    def _runtime(self):
        if self._client is None:
            self._client = self._client_factory()
        return LangGraphRuntime(
            self._client,
            self._tools,
            self._loop_config,
            self._checkpointer,
            approval_enabled=self._approval_gate is not None,
        )

    def lock(self, session_id):
        return self._locks.setdefault(session_id, asyncio.Lock())

    def respond_approval(self, pending_id: str, decision: ApprovalDecision) -> bool:
        return bool(self._approval_gate and self._approval_gate.respond(pending_id, decision))

    async def _history(self, session_id):
        snapshot = await self._runtime().get_state(thread_id_for_session(session_id))
        if snapshot.values:
            return copy.deepcopy(snapshot.values["messages"])
        saved = self._run_store.get_session(session_id) if self._run_store else None
        return (
            saved["history"]
            if saved
            else [
                {"role": "system", "content": system_prompt(any(t.risk for t in self._tools))},
            ]
        )

    async def session_state(self, session_id):
        snapshot = await self._runtime().get_state(thread_id_for_session(session_id))
        if not snapshot.values:
            saved = self._run_store.get_session(session_id) if self._run_store else None
            if not saved:
                raise SessionError(404, "session_not_found", "会话不存在")
            return {
                "session_id": session_id,
                "status": "completed",
                "messages": saved["history"],
                "pending_approvals": [],
                "tool_results": [],
            }
        pending = [i.value for task in snapshot.tasks for i in task.interrupts]
        status = (
            "waiting_approval"
            if pending
            else (
                "running"
                if self.lock(session_id).locked()
                else ("interrupted" if snapshot.next else "completed")
            )
        )
        return {
            "session_id": session_id,
            "status": status,
            "messages": snapshot.values["messages"],
            "pending_approvals": pending,
            "tool_results": snapshot.values.get("tool_history", []),
        }

    async def validate_resume(self, session_id, pending_id):
        if self.lock(session_id).locked():
            raise SessionError(409, "session_busy", "会话已有活动执行，请通过原连接审批")
        state = await self.session_state(session_id)
        if not any(p["pending_id"] == pending_id for p in state["pending_approvals"]):
            raise SessionError(409, "stale_approval", "审批已失效或不属于此会话")

    async def reserve_resume(self, session_id, pending_id):
        await self.validate_resume(session_id, pending_id)
        lock = self.lock(session_id)
        if lock.locked():
            raise SessionError(409, "session_busy", "会话已有活动执行")
        await lock.acquire()
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                lock.release()

        return release

    async def stream_run(self, session_id, message) -> AsyncIterator[AgentEvent]:
        async with self.lock(session_id):
            runtime = self._runtime()
            thread = thread_id_for_session(session_id)
            snapshot = await runtime.get_state(thread)
            if snapshot.next:
                raise SessionError(409, "unfinished_run", "会话尚未完成，请先处理待审批操作")
            previous = await self._history(session_id)
            messages = [*previous, {"role": "user", "content": message}]
            async with aclosing(self._record_run(runtime, messages, session_id, message)) as events:
                async for event in events:
                    yield event

    async def stream_resume(self, session_id, pending_id, approved, reason="", *, reserved=False):
        release = None if reserved else await self.reserve_resume(session_id, pending_id)
        try:
            runtime = self._runtime()
            messages = await self._history(session_id)
            decision = {"pending_id": pending_id, "approved": approved, "reason": reason}
            async with aclosing(
                self._record_run(runtime, messages, session_id, None, decision=decision)
            ) as events:
                async for event in events:
                    yield event
        finally:
            if release:
                release()

    async def stream_retry(self, session_id):
        async with self.lock(session_id):
            runtime = self._runtime()
            thread = thread_id_for_session(session_id)
            snapshot = await runtime.get_state(thread)
            if not snapshot.next:
                raise SessionError(409, "not_interrupted", "会话没有可继续的中断任务")
            if any(task.interrupts for task in snapshot.tasks):
                raise SessionError(409, "approval_required", "会话正在等待审批，请先提交审批决策")
            messages = copy.deepcopy(snapshot.values["messages"])
            async with aclosing(
                self._record_run(runtime, messages, session_id, None, continuation=True)
            ) as events:
                async for event in events:
                    yield event

    async def _drive(self, runtime, messages, thread, decision=None, continuation=False):
        if decision:
            stream = runtime.resume(thread, decision)
        elif continuation:
            stream = runtime.continue_run(thread)
        else:
            stream = runtime.stream(messages, thread_id=thread)
        while True:
            pending, future = None, None
            try:
                async with aclosing(stream) as events:
                    async for event in events:
                        if isinstance(event, ApprovalPending):
                            pending = event
                            future = self._approval_gate.register(event.pending_id)
                        if isinstance(event, LoopEnd):
                            messages[:] = (await runtime.get_state(thread)).values["messages"]
                        yield event
                if pending is None:
                    return
                decision = await future
                stream = runtime.resume(
                    thread,
                    {
                        "pending_id": pending.pending_id,
                        "approved": decision.approved,
                        "reason": decision.reason,
                    },
                )
            finally:
                if pending:
                    self._approval_gate.discard(pending.pending_id)

    async def _record_run(
        self, runtime, messages, session_id, message, *, decision=None, continuation=False
    ):
        thread = thread_id_for_session(session_id)
        self.last_trace = new_trace_path(self._trace_dir, self._model)
        recorder = JsonlTraceRecorder(self.last_trace, self._model, sinks=self._sinks)
        run_id = None
        if self._run_store:
            saved = self._run_store.get_session(session_id)
            run_id = saved["active_run_id"] if saved else None
            if run_id is None and message is not None:
                run_id = self._run_store.create_run(
                    session_id, message, messages[:-1], str(self.last_trace)
                )
        stream = recorder.record(
            self._drive(runtime, messages, thread, decision, continuation), messages
        )
        finished = False
        try:
            async with aclosing(stream) as events:
                async for event in events:
                    if isinstance(event, ApprovalPending) and run_id:
                        snapshot = await runtime.get_state(thread)
                        self._run_store.project_run(
                            run_id, snapshot.values["messages"], "waiting_approval"
                        )
                    if isinstance(event, LoopEnd):
                        if run_id:
                            if event.completed:
                                self._run_store.finish_run(
                                    run_id, messages, str(messages[-1].get("content") or "")
                                )
                            else:
                                self._run_store.fail_run(run_id, "max_steps reached")
                        elif event.completed and self._run_store:
                            self._run_store.update_session_history(session_id, messages)
                        finished = True
                    yield event
        finally:
            if run_id and not finished:
                snapshot = await runtime.get_state(thread)
                if any(task.interrupts for task in snapshot.tasks):
                    self._run_store.project_run(
                        run_id, snapshot.values["messages"], "waiting_approval"
                    )
                else:
                    self._run_store.fail_run(run_id, "execution interrupted")
