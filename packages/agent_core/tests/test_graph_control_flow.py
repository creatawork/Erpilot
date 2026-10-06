from agent_core.context import ContextPolicy
from agent_core.graph import build_graph
from agent_core.graph_state import initial_state
from agent_core.llm import StreamEnd, ToolCall
from agent_core.loop import LoopConfig, StepStarted
from langgraph.checkpoint.memory import InMemorySaver


async def test_max_steps_and_one_based_steps():
    class Client:
        async def stream_chat(self, messages, tools=None):
            yield ToolCall("c", "missing", "{}")
            yield StreamEnd()

    graph = build_graph(Client(), [], LoopConfig(max_steps=2), InMemorySaver())
    events = [
        event
        async for event in graph.astream(
            initial_state([]),
            {"configurable": {"thread_id": "steps"}},
            stream_mode="custom",
        )
    ]
    assert [e.step for e in events if isinstance(e, StepStarted)] == [1, 2]
    state = (await graph.aget_state({"configurable": {"thread_id": "steps"}})).values
    assert state["completed"] is False
    assert state["step"] == 2


async def test_compresses_context_before_model():
    from agent_core.llm import TextDelta

    seen = []

    class Client:
        async def stream_chat(self, messages, tools=None):
            seen.extend(messages)
            yield TextDelta("answer")
            yield StreamEnd()

    messages = [{"role": "system", "content": "system"}]
    for _ in range(10):
        messages.extend(
            [
                {"role": "user", "content": "x" * 100},
                {"role": "assistant", "content": "y" * 100},
            ]
        )
    messages.append({"role": "user", "content": "latest"})
    graph = build_graph(
        Client(),
        [],
        LoopConfig(context=ContextPolicy(max_tokens=100, keep_last_messages=3)),
        InMemorySaver(),
    )
    await graph.ainvoke(initial_state(messages), {"configurable": {"thread_id": "context"}})
    assert len(seen) <= 5
    assert seen[0]["role"] == "system"
    assert seen[-1]["content"] == "latest"
