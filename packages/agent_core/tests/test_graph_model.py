from agent_core.graph import build_graph
from agent_core.graph_state import initial_state
from agent_core.llm import StreamEnd, TextDelta, Usage
from agent_core.loop import LoopConfig
from langgraph.checkpoint.memory import InMemorySaver


class Client:
    async def stream_chat(self, messages, tools=None):
        yield TextDelta("答复")
        yield StreamEnd(Usage(3, 2, 5))


async def test_model_uses_existing_client_and_checkpointer():
    saver = InMemorySaver()
    graph = build_graph(Client(), [], LoopConfig(), saver)
    config = {"configurable": {"thread_id": "model"}}
    result = await graph.ainvoke(initial_state([{"role": "user", "content": "问"}]), config)
    assert result["messages"][-1] == {"role": "assistant", "content": "答复"}
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert result["completed"] is True
    assert (await graph.aget_state(config)).values["final_answer"] == "答复"
