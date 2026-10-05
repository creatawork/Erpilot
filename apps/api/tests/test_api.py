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
            return sse_response(
                [
                    chunk(delta={"content": "订单 123 已发货"}),
                    chunk(usage=USAGE),
                ]
            )
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
        "start",
        "step",
        "tool_started",
        "tool_finished",
        "step",
        "delta",
        "done",
    ]
    assert events[0][1]["model"] == "glm-5.3-flash"
    steps = [data for name, data in events if name == "step"]
    assert steps == [{"step": 1}, {"step": 2}]
    tool_finished = next(data for name, data in events if name == "tool_finished")
    assert tool_finished["ok"] is True and tool_finished["id"] == "call_1"
    done = next(data for name, data in events if name == "done")
    assert done["steps"] == 2 and done["completed"] is True
    assert done["usage"] == {  # 两步 usage 合计
        "prompt_tokens": 26,
        "completion_tokens": 14,
        "total_tokens": 40,
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


async def test_session_history_survives_service_reconstruction(tmp_path) -> None:
    from agent_core.demo_tools import DEMO_TOOLS
    from erpilot_api.run_store import RunStore
    from erpilot_api.service import ChatService

    requests: list[httpx2.Request] = []
    store = RunStore(tmp_path / "runs.db")
    kwargs = dict(
        client_factory=lambda: make_client(_handler_pair(requests)),
        model="glm-5.3-flash", trace_dir=tmp_path, tools=DEMO_TOOLS, run_store=store,
    )
    first = ChatService(**kwargs)
    async for _ in first.stream_run("s1", "查订单 123"):
        pass
    assert store.get_session("s1")["history"][-1]["content"] == "订单 123 已发货"

    second = ChatService(**{**kwargs, "run_store": RunStore(tmp_path / "runs.db")})
    async for _ in second.stream_run("s1", "再查一次"):
        pass
    roles = [m["role"] for m in requests[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]


async def test_llm_error_becomes_error_event(tmp_path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "bad request"}})

    app = create_app(client_factory=lambda: make_client(handler), trace_dir=tmp_path)
    events = await _post_sse(app, {"message": "hi"})

    names = [name for name, _ in events]
    assert "error" in names and "done" not in names
    error = next(data for name, data in events if name == "error")
    assert "400" in error["message"]


async def test_chat_stream_uses_injected_tools(tmp_path) -> None:
    """ChatService 的工具集来自注入（M3 第 2 周），api 本体不硬编码工具来源。"""
    from agent_core.testing import tool_call_chunks as _tcc
    from agent_core.tools import tool
    from pydantic import BaseModel

    class Ping(BaseModel):
        word: str

    @tool(name="echo_ping", description="回声测试", params=Ping)
    async def echo_ping(params: Ping) -> dict[str, str]:
        return {"echo": params.word}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])
        return sse_response(_tcc("call_9", "echo_ping", '{"word": "hi"}'))

    app = create_app(
        client_factory=lambda: make_client(handler), trace_dir=tmp_path, tools=[echo_ping]
    )
    events = await _post_sse(app, {"message": "测试工具注入"})

    started = next(data for name, data in events if name == "tool_started")
    assert started["name"] == "echo_ping"
    finished = next(data for name, data in events if name == "tool_finished")
    assert json.loads(finished["content"]) == {"echo": "hi"}


async def test_trace_written_to_configured_dir(tmp_path) -> None:
    requests: list[httpx2.Request] = []
    await _post_sse(_app(requests, tmp_path), {"message": "查订单 123 的状态"})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines() if ln]
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_end"
    assert records[-1]["steps"] == 2 and records[-1]["completed"] is True


# ---- 挂起式审批（M4 第 2 周，ADR-0006） ----

from agent_core.approval import RISK_SINGLE_CONFIRM, StreamApprovalGate, guarded  # noqa: E402
from agent_core.tools import tool as tool_dec  # noqa: E402
from pydantic import BaseModel as _BM  # noqa: E402


class _WriteIn(_BM):
    sku: str


@tool_dec(
    name="create_order_api",
    description="建单（需审批）",
    params=_WriteIn,
    risk=RISK_SINGLE_CONFIRM,
)
async def _create_order_api(params: _WriteIn) -> dict[str, str]:
    return {"created": params.sku}


def _write_handler(requests: list[httpx2.Request]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse_response([chunk(delta={"content": "已创建订单"}), chunk(usage=USAGE)])
        return sse_response(tool_call_chunks("call_w", "create_order_api", '{"sku": "A1001"}'))

    return handler


def _parse_block(block: str) -> tuple[str, dict] | None:
    lines = re.split(r"\r?\n", block)
    name = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
    data = next((ln[6:] for ln in lines if ln.startswith("data: ")), None)
    if name and data:
        return (name, json.loads(data))
    return None


async def test_approval_flow_via_chat_service(tmp_path) -> None:
    """服务层挂起流：stream_run 在 approval_pending 处等待，决策回填后继续推进。

    ASGITransport 会缓冲完整响应体，挂起的 SSE 流无法在 HTTP 边界做增量
    读取——挂起语义在服务层直测（loop 层等价覆盖在 agent_core 测试里），
    SSE 帧编码由 test_encode_approval_events 单测把关。
    """
    from agent_core.approval import ApprovalDecision
    from agent_core.loop import ApprovalPending
    from erpilot_api.service import ChatService

    requests: list[httpx2.Request] = []
    gate = StreamApprovalGate()
    service = ChatService(
        client_factory=lambda: make_client(_write_handler(requests)),
        model="glm-5.3-flash",
        trace_dir=tmp_path,
        tools=[guarded(_create_order_api, gate)],
        approval_gate=gate,
    )
    events = []
    async for event in service.stream_run("s1", "下单 A1001"):
        if isinstance(event, ApprovalPending):
            assert event.arguments == {"sku": "A1001"}
            assert service.respond_approval(event.pending_id, ApprovalDecision(approved=True))
        events.append(event)

    names = [type(e).__name__ for e in events]
    assert "ApprovalPending" in names and "ApprovalResolved" in names
    assert names[-1] == "LoopEnd"
    finished = next(e for e in events if type(e).__name__ == "ToolCallFinished")
    assert json.loads(finished.content) == {"created": "A1001"}
    from agent_core.demo_tools import WRITES_PROMPT

    assert requests[0]["messages"][0]["content"] == WRITES_PROMPT


async def test_encode_approval_events() -> None:
    """approval_pending / approval_resolved 的 SSE 帧编码（协议 v1 扩展）。"""
    from agent_core.approval import RISK_SINGLE_CONFIRM as RISK
    from agent_core.loop import ApprovalPending, ApprovalResolved
    from erpilot_api.events import encode_event

    pending = ApprovalPending(
        call_id="call_w",
        pending_id="p1",
        tool="create_order",
        risk=RISK,
        arguments={"sku": "A1001"},
    )
    assert encode_event(pending) == (
        "approval_pending",
        {
            "call_id": "call_w",
            "pending_id": "p1",
            "tool": "create_order",
            "risk": RISK,
            "arguments": {"sku": "A1001"},
        },
    )
    resolved = ApprovalResolved(
        call_id="call_w",
        pending_id="p1",
        tool="create_order",
        approved=False,
        reason="额度不足",
    )
    assert encode_event(resolved) == (
        "approval_resolved",
        {
            "call_id": "call_w",
            "pending_id": "p1",
            "tool": "create_order",
            "approved": False,
            "reason": "额度不足",
        },
    )


async def test_approve_unknown_pending_id_returns_ok_false(tmp_path) -> None:
    app = create_app(
        client_factory=lambda: make_client(_write_handler([])),
        trace_dir=tmp_path,
        tools=[],  # 无写工具 → 无门
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/approve", json={"pending_id": "nope", "approved": True})
    assert resp.json() == {"ok": False}


async def test_closing_service_stream_releases_approval_and_repairs_history(tmp_path):
    from agent_core.loop import ApprovalPending
    from erpilot_api.service import ChatService

    gate = StreamApprovalGate()
    service = ChatService(
        client_factory=lambda: make_client(_write_handler([])),
        model="glm-5.3-flash", trace_dir=tmp_path,
        tools=[guarded(_create_order_api, gate)], approval_gate=gate,
    )
    stream = service.stream_run("s1", "write")
    async for event in stream:
        if isinstance(event, ApprovalPending):
            await stream.aclose()
            break
    assert not gate._waiters
    assert [m["role"] for m in service._sessions["s1"]] == ["system"]
