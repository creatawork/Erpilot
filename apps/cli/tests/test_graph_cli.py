from agent_core.approval import AutoDenyGate, guarded
from agent_core.testing import chunk, make_client, sse_response, tool_call_chunks
from agent_core.tools import Tool
from erpilot_cli import main
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel


def test_cli_uses_explicit_graph_saver(monkeypatch, tmp_path):
    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.llm import LLMConfig

    captured = []

    def runtime(*args, **kwargs):
        assert isinstance(kwargs.get("checkpointer"), InMemorySaver)
        captured.append(kwargs["checkpointer"])
        return LangGraphRuntime(*args, **kwargs)

    monkeypatch.setattr(main, "LangGraphRuntime", runtime, raising=False)
    monkeypatch.setattr(main.LLMConfig, "from_env", lambda: LLMConfig(api_key="test"))
    monkeypatch.setattr(
        main,
        "LLMClient",
        lambda config: make_client(
            lambda req: sse_response([chunk(delta={"content": "done"}), chunk(usage=None)])
        ),
    )
    main.chat(prompt=["test"], tools_mode="demo", trace_dir=tmp_path, writes=False)
    assert len(captured) == 1


async def test_trace_can_consume_graph_with_scripted_denial(tmp_path):
    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.trace import JsonlTraceRecorder, load_records

    class Params(BaseModel):
        value: int
        client_token: str | None = None

    async def handler(args):
        raise AssertionError("denied write cannot run")

    import json

    def llm(req):
        messages = json.loads(req.content)["messages"]
        if messages[-1]["role"] == "tool":
            return sse_response([chunk(delta={"content": "未执行"}), chunk(usage=None)])
        return sse_response(tool_call_chunks("w", "write", '{"value":7}'))

    runtime = LangGraphRuntime(
        make_client(llm),
        [guarded(Tool("write", "write", Params, handler, risk="confirm"), AutoDenyGate())],
        checkpointer=InMemorySaver(),
    )
    recorder = JsonlTraceRecorder(tmp_path / "trace.jsonl", "model")
    events = [
        e
        async for e in recorder.run(
            runtime, [{"role": "user", "content": "write"}], thread_id="trace"
        )
    ]
    assert events[-1].completed
    records = load_records(tmp_path / "trace.jsonl")
    assert [r["type"] for r in records if r["type"].startswith("approval")] == [
        "approval_pending",
        "approval_resolved",
    ]
    assert records[-1]["messages"][-1]["content"] == "未执行"
