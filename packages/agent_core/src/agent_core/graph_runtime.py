"""Checkpointed runtime and adapter to Erpilot's existing event protocol."""

from contextlib import aclosing
from dataclasses import replace
from uuid import uuid4

from langgraph.types import Command

from agent_core.approval import ApprovalRequest
from agent_core.events import ApprovalPending, LoopEnd
from agent_core.graph import build_graph
from agent_core.graph_approval import ResumeDecision
from agent_core.graph_state import initial_state
from agent_core.llm import Usage
from agent_core.runtime_config import LoopConfig


class LangGraphRuntime:
    def __init__(self, client, tools=(), config=None, checkpointer=None, *, approval_enabled=False):
        if checkpointer is None:
            raise ValueError("explicit checkpointer is required")
        self._config = config or LoopConfig()
        self._gates = {t.name: t.approval_gate for t in tools if t.approval_gate is not None}
        approval_enabled = approval_enabled or all(
            t.risk is None or t.approval_gate is not None for t in tools
        )
        tools = [replace(t, handler=t.approved_handler) if t.approved_handler else t for t in tools]
        self.graph = build_graph(
            client, list(tools), self._config, checkpointer, approval_enabled=approval_enabled
        )

    def _checkpoint_config(self, thread_id):
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(25, self._config.max_steps * 6 + 10),
        }

    async def get_state(self, thread_id):
        return await self.graph.aget_state(self._checkpoint_config(thread_id))

    async def stream(self, messages, *, thread_id):
        snapshot = await self.get_state(thread_id)
        if snapshot.next:
            raise ValueError("thread has unfinished execution; resume it before a new message")
        async with aclosing(self._stream(initial_state(messages), thread_id)) as events:
            async for event in events:
                yield event

    async def resume(self, thread_id, decision):
        decision = ResumeDecision.model_validate(decision)
        snapshot = await self.get_state(thread_id)
        pending = [i.value for task in snapshot.tasks for i in task.interrupts]
        if not any(p["pending_id"] == decision.pending_id for p in pending):
            raise ValueError("unknown or stale approval pending_id")
        async with aclosing(
            self._stream(Command(resume=decision.model_dump()), thread_id)
        ) as events:
            async for event in events:
                yield event

    async def continue_run(self, thread_id):
        """Continue a checkpointed node after a transient runtime interruption."""
        snapshot = await self.get_state(thread_id)
        if not snapshot.next:
            raise ValueError("thread has no interrupted execution to continue")
        if any(task.interrupts for task in snapshot.tasks):
            raise ValueError("thread is waiting for an approval decision")
        async with aclosing(self._stream(None, thread_id)) as events:
            async for event in events:
                yield event

    async def _stream(self, value, thread_id):
        async with aclosing(
            self.graph.astream(
                value,
                self._checkpoint_config(thread_id),
                stream_mode="custom",
            )
        ) as events:
            async for event in events:
                yield event
        # Only expose pending after the interrupt's checkpoint has been persisted.
        snapshot = await self.get_state(thread_id)
        pending = [i.value for task in snapshot.tasks for i in task.interrupts]
        if pending:
            for payload in pending:
                yield ApprovalPending(**payload)
        elif not snapshot.next:
            state = snapshot.values
            yield LoopEnd(
                state["step"],
                Usage(**state["usage"]) if state["usage"] else None,
                state["completed"],
            )

    async def run(self, messages, *, thread_id=None):
        """Consume a conversation with explicitly configured live or scripted gates."""
        thread_id = thread_id or uuid4().hex
        stream = self.stream(messages, thread_id=thread_id)
        while True:
            pending = None
            future = None
            gate = None
            try:
                async with aclosing(stream) as events:
                    async for event in events:
                        if isinstance(event, ApprovalPending):
                            pending = event
                            gate = self._gates.get(event.tool)
                            if gate is not None and hasattr(gate, "register"):
                                future = gate.register(event.pending_id)
                        if isinstance(event, LoopEnd):
                            messages[:] = (await self.get_state(thread_id)).values["messages"]
                        yield event
                if pending is None:
                    return
                if future is not None:
                    decision = await future
                elif gate is not None and hasattr(gate, "review"):
                    decision = await gate.review(
                        ApprovalRequest(
                            pending.tool,
                            pending.risk,
                            pending.arguments,
                        )
                    )
                else:
                    return
                stream = self.resume(
                    thread_id,
                    {
                        "pending_id": pending.pending_id,
                        "approved": decision.approved,
                        "reason": decision.reason,
                    },
                )
            finally:
                if pending is not None and gate is not None and hasattr(gate, "discard"):
                    gate.discard(pending.pending_id)
