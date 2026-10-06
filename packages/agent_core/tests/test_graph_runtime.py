from dataclasses import asdict

from agent_core.graph_runtime import LangGraphRuntime
from agent_core.llm import StreamEnd, TextDelta, Usage
from agent_core.loop import LoopEnd, StepEnd, StepStarted
from langgraph.checkpoint.memory import InMemorySaver


async def test_runtime_preserves_events_and_message_contract():
    class Client:
        async def stream_chat(self, messages, tools=None):
            yield TextDelta("answer")
            yield StreamEnd(Usage(3, 2, 5))

    runtime = LangGraphRuntime(Client(), checkpointer=InMemorySaver())
    messages = [{"role": "user", "content": "question"}]
    events = [e async for e in runtime.stream(messages, thread_id="runtime")]
    assert [type(e) for e in events] == [StepStarted, TextDelta, StepEnd, LoopEnd]
    assert asdict(events[-1]) == {
        "steps": 1,
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
        "completed": True,
    }
    snapshot = await runtime.get_state("runtime")
    assert snapshot.values["messages"][-1] == {"role": "assistant", "content": "answer"}


async def test_runtime_resume_uses_fixed_pending_state():
    from agent_core.events import ApprovalPending, ApprovalResolved
    from agent_core.tools import Tool
    from test_graph_approval import Client, WriteParams

    calls = []

    async def handler(args):
        calls.append(args.model_dump())
        return "written"

    runtime = LangGraphRuntime(
        Client(),
        [Tool("write", "write", WriteParams, handler, risk="confirm")],
        checkpointer=InMemorySaver(),
        approval_enabled=True,
    )
    events = [e async for e in runtime.stream([], thread_id="pending")]
    pending = events[-1]
    assert isinstance(pending, ApprovalPending)
    assert not any(isinstance(e, LoopEnd) for e in events)
    resumed = [
        e
        async for e in runtime.resume(
            "pending",
            {
                "pending_id": pending.pending_id,
                "approved": True,
            },
        )
    ]
    assert isinstance(resumed[0], ApprovalResolved)
    assert calls == [pending.arguments]
    assert isinstance(resumed[-1], LoopEnd)
