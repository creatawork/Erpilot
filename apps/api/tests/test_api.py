"""API 链路单测：SSE 事件协议、会话历史延续、trace 落盘、流内错误上报。

LLM 在 HTTP 边界 mock（agent_core.testing），整条 FastAPI → AgentLoop → trace
链路都跑真代码，不烧 token。
"""

import json
import re

import httpx2
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from erpilot_api.main import create_app


def _handler_pair(requests: list[httpx2.Request]):
    """两段式端点：首轮返回工具调用，见到 tool 消息后返回最终文本。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([
                chunk(delta={"content": "订单 123 已发货"}),
                chunk(usage=USAGE),
            ])
        return sse_response(tool_call_chunks("call_1", "get_order_status", '{"order_id": "123"}'))

    return handler


def _app(requests: list[httpx2.Request], tmp_path):
    return create_app(
        client_factory=lambda: make_client(_handler_pair(requests)),
        trace_dir=tmp_path,
    )


async def _post_sse(app, payload: dict) -> list[tuple[str, dict]]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events: list[tuple[str, dict]] = []
    for block in re.split(r"\r?\n\r?\n", resp.text):
        lines = re.split(r"\r?\n", block)
        name = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
        data = next((ln[6:] for ln in lines if ln.startswith("data: ")), None)
        if name and data:
            events.append((name, json.loads(data)))
    return events


async def test_healthz(tmp_path) -> None:
    transport = httpx2.ASGITransport(app=_app([], tmp_path))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_chat_stream_emits_protocol_events(tmp_path) -> None:
    requests: list[httpx2.Request] = []
    events = await _post_sse(_app(requests, tmp_path), {"message": "查订单 123 的状态"})

    assert [name for name, _ in events] == [
        "start", "step", "tool_started", "tool_finished", "step", "delta", "done",
    ]
    assert events[0][1]["model"] == "glm-5.3-flash"
    steps = [data for name, data in events if name == "step"]
    assert steps == [{"step": 1}, {"step": 2}]
    tool_finished = next(data for name, data in events if name == "tool_finished")
    assert tool_finished["ok"] is True and tool_finished["id"] == "call_1"
    done = next(data for name, data in events if name == "done")
    assert done["steps"] == 2 and done["completed"] is True
    assert done["usage"] == {  # 两步 usage 合计
        "prompt_tokens": 26, "completion_tokens": 14, "total_tokens": 40,
    }
    assert done["cost"] > 0 and done["duration_ms"] >= 0
    assert done["trace"].endswith(".jsonl")


async def test_session_history_persists_across_calls(tmp_path) -> None:
    requests: list[httpx2.Request] = []
    app = _app(requests, tmp_path)

    first = await _post_sse(app, {"message": "查订单 123 的状态"})
    session_id = first[0][1]["session_id"]
    second = await _post_sse(app, {"message": "那报价多少？", "session_id": session_id})

    assert second[0][1]["session_id"] == session_id  # 复用同一会话
    # 第一轮 run 发了 2 个 LLM 请求；第二轮 run 的首个请求历史应为：
    # system → user → assistant(tool_calls) → tool → assistant(最终回答) → user(新消息)
    roles = [m["role"] for m in requests[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert "那报价多少" in json.dumps(requests[2], ensure_ascii=False)
    done = next(data for name, data in second if name == "done")
    assert done["completed"] is True


async def test_llm_error_becomes_error_event(tmp_path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "bad request"}})

    app = create_app(client_factory=lambda: make_client(handler), trace_dir=tmp_path)
    events = await _post_sse(app, {"message": "hi"})

    names = [name for name, _ in events]
    assert "error" in names and "done" not in names
    error = next(data for name, data in events if name == "error")
    assert "400" in error["message"]


async def test_trace_written_to_configured_dir(tmp_path) -> None:
    requests: list[httpx2.Request] = []
    await _post_sse(_app(requests, tmp_path), {"message": "查订单 123 的状态"})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines() if ln]
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_end"
    assert records[-1]["steps"] == 2 and records[-1]["completed"] is True
