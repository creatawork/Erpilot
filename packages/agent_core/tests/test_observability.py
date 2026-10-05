"""observability 单测：LangfuseTraceSink 的记录映射 + recorder 双写转发。

全部用 fake client（duck-typed），不依赖 langfuse SDK 与网络；真实端到端
联调在自托管 Langfuse 部署后进行（keys 当前为空，属预期路径）。
"""

import json
from collections.abc import Callable

import httpx2
import pytest
from agent_core.loop import AgentLoop
from agent_core.observability import LangfuseTraceSink, langfuse_sink_from_env
from agent_core.testing import BASE_URL, chunk, make_client, sse_response, tool_call_chunks
from agent_core.tools import tool
from agent_core.trace import JsonlTraceRecorder, load_records
from openai import APIStatusError
from pydantic import BaseModel


class FakeSpan:
    """span/generation 的共同 fake：真实 SDK 里两者都支持挂子 span。"""

    def __init__(self, log: list, name: str, **kwargs) -> None:
        self.log, self.name = log, name

    def span(self, name: str, **kwargs) -> "FakeSpan":
        self.log.append(("span", name, kwargs))
        return FakeSpan(self.log, name)

    def generation(self, name: str, **kwargs) -> "FakeSpan":
        self.log.append(("generation", name, kwargs))
        return FakeSpan(self.log, name)

    def end(self) -> None:
        self.log.append(("end", self.name))


class FakeTrace:
    def __init__(self, log: list) -> None:
        self.log = log

    def span(self, name: str, **kwargs) -> FakeSpan:
        self.log.append(("span", name, kwargs))
        return FakeSpan(self.log, name)

    def generation(self, name: str, **kwargs) -> FakeSpan:
        self.log.append(("generation", name, kwargs))
        return FakeSpan(self.log, name)

    def update(self, **kwargs) -> None:
        self.log.append(("update", kwargs.pop("level", None), kwargs))


class FakeLangfuse:
    def __init__(self) -> None:
        self.log: list = []
        self.flushed = 0

    def trace(self, name: str, **kwargs) -> FakeTrace:
        self.log.append(("trace", name, kwargs))
        return FakeTrace(self.log)

    def flush(self) -> None:
        self.flushed += 1


def _feed(sink: LangfuseTraceSink) -> None:
    """按 trace.py 记录模型喂一遍正常完成的 run（一步含一次工具调用）。"""
    sink.write({"type": "run_start", "run_id": "abc123", "model": "glm-5.3-flash",
                "ts": "t", "messages": [{"role": "user", "content": "q"}]})
    sink.write({"type": "step_start", "run_id": "abc123", "step": 1, "ts": "t"})
    sink.write({"type": "tool_call", "run_id": "abc123", "step": 1, "ts": "t",
                "id": "c1", "name": "get_order", "arguments": "{}",
                "content": "{}", "ok": True, "duration_ms": 1.0})
    sink.write({"type": "step_end", "run_id": "abc123", "step": 1, "ts": "t",
                "text": "查到了", "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                            "total_tokens": 15},
                "cost": 0.001, "duration_ms": 9.0})
    sink.write({"type": "run_end", "run_id": "abc123", "ts": "t", "steps": 1,
                "completed": True, "usage": {"total_tokens": 15}, "cost": 0.001,
                "duration_ms": 20.0, "messages": [{"role": "assistant", "content": "查到了"}]})


def test_normal_run_mapping_and_flush() -> None:
    lf = FakeLangfuse()
    sink = LangfuseTraceSink(client=lf)
    _feed(sink)
    sink.close()

    log = lf.log
    # run → trace（session_id = run_id），step → span + generation，tool_call → span
    assert log[0] == ("trace", "erpilot-agent-run",
                      {"session_id": "abc123", "input": [{"role": "user", "content": "q"}],
                       "metadata": {"model": "glm-5.3-flash"}})
    assert any(e[0] == "span" and e[1] == "step-1" for e in log)
    assert any(e[0] == "span" and e[1] == "tool:get_order" for e in log)
    gen = next(e for e in log if e[0] == "generation")
    assert gen[1] == "step-1"
    assert gen[2]["model"] == "glm-5.3-flash"
    assert gen[2]["usage"] == {"input": 10, "output": 5, "total": 15}
    assert gen[2]["metadata"] == {"cost": 0.001, "duration_ms": 9.0}
    # run_end → update（带最终回答与 metadata.completed），收尾后 close → flush 一次
    assert any(e[0] == "update" and e[1] is None and e[2].get("output") == "查到了"
               and e[2].get("metadata", {}).get("completed") is True for e in log)
    assert lf.flushed == 1


def test_run_error_marks_level_error() -> None:
    lf = FakeLangfuse()
    sink = LangfuseTraceSink(client=lf)
    sink.write({"type": "run_start", "run_id": "x", "model": "m", "ts": "t", "messages": []})
    sink.write({"type": "run_error", "run_id": "x", "ts": "t",
                "error": "APIError: 502", "duration_ms": 5.0})
    assert any(e[0] == "update" and e[1] == "ERROR" for e in lf.log)


def test_write_swallows_sink_failures() -> None:
    class Broken(FakeLangfuse):
        def trace(self, *args, **kwargs):
            raise RuntimeError("langfuse down")

    sink = LangfuseTraceSink(client=Broken())
    sink.write({"type": "run_start", "run_id": "x", "model": "m", "ts": "t", "messages": []})
    sink.close()  # 不抛即通过：上报失败不能反噬业务


def test_records_after_run_ended_are_ignored() -> None:
    lf = FakeLangfuse()
    sink = LangfuseTraceSink(client=lf)
    sink.write({"type": "run_start", "run_id": "x", "model": "m", "ts": "t", "messages": []})
    sink.write({"type": "run_error", "run_id": "x", "ts": "t", "error": "e", "duration_ms": 1})
    before = len(lf.log)
    sink.write({"type": "step_start", "run_id": "x", "step": 1, "ts": "t"})
    assert len(lf.log) == before  # run 已终结，后续记录不产生新对象


def test_langfuse_sink_from_env_disabled_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert langfuse_sink_from_env() is None


# ---- recorder 双写：本地文件 + sink 转发，异常路径也 close ----


class OrderQuery(BaseModel):
    order_id: str


@tool(name="get_order_status", description="查询订单状态", params=OrderQuery)
async def get_order_status(params: OrderQuery) -> dict[str, str]:
    return {"status": "已发货"}


def _two_step_handler() -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([
                chunk(delta={"content": "已发货"}),
                chunk(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
            ])
        return sse_response(
            tool_call_chunks("call_1", "get_order_status", '{"order_id": "123"}')
        )

    return handler


def _failing_handler() -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        req = httpx2.Request("POST", BASE_URL + "chat/completions", json={})
        return httpx2.Response(500, request=req, content=b"boom")

    return handler


async def _run(tmp_path, handler, sinks: list) -> None:
    agent = AgentLoop(make_client(handler), tools=[get_order_status])
    recorder = JsonlTraceRecorder(
        tmp_path / "run.jsonl", model="glm-5.3-flash", sinks=sinks
    )
    return [e async for e in recorder.run(agent, [{"role": "user", "content": "q"}])]


async def test_recorder_forwards_records_and_closes_sink(tmp_path) -> None:
    lf = FakeLangfuse()
    sink = LangfuseTraceSink(client=lf)
    await _run(tmp_path, _two_step_handler(), [sink])

    records = load_records(tmp_path / "run.jsonl")
    assert records[-1]["type"] == "run_end"  # 本地留档完整
    assert any(e[0] == "trace" for e in lf.log)  # 远程侧收到全流程
    assert any(e[0] == "update" and e[2].get("metadata", {}).get("completed")
               for e in lf.log)
    assert lf.flushed == 1  # run 结束后自动 close


async def test_recorder_closes_sink_on_run_error(tmp_path) -> None:
    lf = FakeLangfuse()
    sink = LangfuseTraceSink(client=lf)
    with pytest.raises(APIStatusError):
        await _run(tmp_path, _failing_handler(), [sink])

    assert lf.flushed == 1  # 异常路径 sink 也被 close，本地 run_error 已落盘
    assert any(e[0] == "update" and e[1] == "ERROR" for e in lf.log)
