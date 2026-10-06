import pytest
from agent_core.graph import build_graph
from agent_core.graph_state import initial_state
from agent_core.runtime_config import LoopConfig
from agent_core.tools import Tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from test_graph_approval import Client, WriteParams


async def test_business_commit_before_result_checkpoint_replays_original_token():
    class FailingSaver(InMemorySaver):
        fail = False

        async def aput(self, config, checkpoint, metadata, new_versions):
            if self.fail and checkpoint["channel_values"].get("tool_results"):
                self.fail = False
                raise RuntimeError("result checkpoint unavailable")
            return await super().aput(config, checkpoint, metadata, new_versions)

    saver = FailingSaver()
    received, committed = [], {}

    async def handler(args):
        received.append(args.model_dump())
        committed.setdefault(args.client_token, {"written": args.value})
        saver.fail = True
        return committed[args.client_token]

    tools = [Tool("write", "write", WriteParams, handler, risk="confirm", retry_safe=True)]
    config = {"configurable": {"thread_id": "crash"}}
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    pending = (await graph.ainvoke(initial_state([]), config))["__interrupt__"][0].value
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await graph.ainvoke(
            Command(resume={"pending_id": pending["pending_id"], "approved": True}), config
        )
    assert received == [pending["arguments"]]
    # Saver pending writes can recover a completed node without replaying its handler.
    saver.fail = False
    graph = build_graph(Client(), tools, LoopConfig(), saver, approval_enabled=True)
    result = await graph.ainvoke(None, config)
    assert result["completed"]
    assert len(committed) == 1
    assert all(args == pending["arguments"] for args in received)
