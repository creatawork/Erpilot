"""阶段二 API 测试：展示事件持久化、快照 presentation 投影、reasoning/display 编码、心跳。"""

import json
import sqlite3

from agent_core.events import ReasoningDelta, ToolCallFinished
from erpilot_api.presentation import build_presentation
from erpilot_api.run_store import RunStore

# ---- 展示事件表 ----


def test_presentation_events_roundtrip_dedup_and_prune(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_run("s1", "查库存", [], None)
    run_id = store.get_session("s1")["active_run_id"]

    assert store.next_presentation_seq(run_id) == 1

    def make(seq, etype):
        return {
            "event_id": f"e{seq}", "session_id": "s1", "run_id": run_id,
            "segment_id": "seg1", "seq": seq, "type": etype, "payload": {},
        }

    store.append_presentation_events([make(1, "user_message"), make(2, "step")])
    store.append_presentation_events([make(3, "delta")])
    assert store.next_presentation_seq(run_id) == 4

    # event_id 冲突被忽略（重放去重）
    store.append_presentation_events([make(3, "delta")])
    assert len(store.read_presentation_events("s1")) == 3

    # 按 seq 排序输出
    events = store.read_presentation_events("s1")
    assert [e["seq"] for e in events] == [1, 2, 3]

    # 活动 run 的展示事件即使过期也保留；结束后才按保留期清理。
    store.finish_run(run_id, [], "done")
    conn = sqlite3.connect(tmp_path / "runs.db")
    conn.execute("UPDATE presentation_event SET occurred_at = datetime('now', '-31 days')")
    conn.commit()
    conn.close()
    store.prune_presentation()
    assert store.read_presentation_events("s1") == []


# ---- 快照 presentation 投影 ----


def _events(*triples):
    return [
        {
            "event_id": f"e{i}", "run_id": "r1", "segment_id": "seg",
            "seq": i, "type": t, "payload": p, "occurred_at": "2026-10-07 10:00:00",
        }
        for i, (t, p) in enumerate(triples, 1)
    ]


def test_projection_replays_blocks_tools_and_terminal_state() -> None:
    events = _events(
        ("user_message", {"text": "下单 A1001"}),
        ("step", {"step": 1}),
        ("delta", {"text": "正在"}),
        ("delta", {"text": "下单"}),
        ("tool_started", {"id": "w1", "name": "create_order", "arguments": '{"sku":"A1001"}'}),
        ("approval_pending", {"call_id": "w1", "pending_id": "p1", "tool": "create_order",
                              "risk": "confirm", "arguments": {"sku": "A1001"}}),
        ("approval_resolved", {"call_id": "w1", "pending_id": "p1", "tool": "create_order",
                               "approved": True, "reason": ""}),
        ("tool_executing", {"id": "w1", "name": "create_order"}),
        ("tool_finished", {"id": "w1", "name": "create_order", "ok": True, "content": "{}",
                           "display": {"version": 1, "kind": "order_creation",
                                       "outcome": "succeeded", "order_id": "SO-1"}}),
        ("done", {"steps": 2, "completed": True}),
    )
    presentation = build_presentation(events)
    assert presentation["version"] == 1 and presentation["last_seq"] == 10
    turn = presentation["turns"][0]
    assert turn["user_text"] == "下单 A1001"
    assert [b["type"] for b in turn["blocks"]] == ["text", "tool"]
    assert turn["blocks"][0]["text"] == "正在下单"
    entity = turn["tools"]["w1"]
    assert entity["status"] == "succeeded"
    assert entity["approval"]["pending_id"] == "p1"
    assert entity["approvalResolved"]["approved"] is True
    assert entity["finished"]["display"]["kind"] == "order_creation"
    assert turn["terminal"] is True and turn["done"]["completed"] is True


def test_projection_denied_tool_never_shows_success() -> None:
    events = _events(
        ("user_message", {"text": "改库存"}),
        ("tool_started", {"id": "w", "name": "adjust_stock", "arguments": "{}"}),
        ("approval_resolved", {"call_id": "w", "pending_id": "p", "tool": "adjust_stock",
                               "approved": False, "reason": "不批"}),
        ("tool_finished", {"id": "w", "name": "adjust_stock", "ok": True,
                           "content": json.dumps({"approval": "denied"}), "display": None}),
    )
    turn = build_presentation(events)["turns"][0]
    assert turn["tools"]["w"]["status"] == "denied"


def test_projection_uses_business_outcome_instead_of_transport_ok() -> None:
    for outcome in ("failed", "unknown"):
        events = _events(
            ("user_message", {"text": "改库存"}),
            ("tool_started", {"id": "w", "name": "adjust_stock", "arguments": "{}"}),
            ("tool_finished", {"id": "w", "name": "adjust_stock", "ok": True,
                               "content": "{}", "display": {
                                   "version": 1, "kind": "stock_adjustment", "outcome": outcome,
                               }}),
        )
        assert build_presentation(events)["turns"][0]["tools"]["w"]["status"] == outcome


def test_projection_recognizes_business_error_without_display() -> None:
    events = _events(
        ("user_message", {"text": "改库存"}),
        ("tool_started", {"id": "w", "name": "adjust_stock", "arguments": "{}"}),
        ("tool_finished", {"id": "w", "name": "adjust_stock", "ok": True,
                           "content": '{"error":{"code":"not_found"}}'}),
    )
    assert build_presentation(events)["turns"][0]["tools"]["w"]["status"] == "failed"


def test_projection_keeps_unconfirmed_business_results_unknown() -> None:
    events = _events(
        ("user_message", {"text": "查询"}),
        ("tool_started", {"id": "w", "name": "get_stock", "arguments": "{}"}),
        ("tool_finished", {"id": "w", "name": "get_stock", "ok": True, "content": "{}"}),
    )
    assert build_presentation(events)["turns"][0]["tools"]["w"]["status"] == "unknown"


def test_projection_unfinished_run_marks_partial_output() -> None:
    events = _events(
        ("user_message", {"text": "hi"}),
        ("delta", {"text": "写到一半"}),
    )
    turn = build_presentation(events)["turns"][0]
    assert turn["terminal"] is False


def test_projection_ignores_orphans_and_missing_runs() -> None:
    assert build_presentation([]) is None
    # 没有 user_message 归属的事件不产生轮次
    assert build_presentation(_events(("step", {"step": 1})))["turns"] == []


def test_session_projection_keeps_run_local_sequences_separate(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    # Deliberately reverse lexical run IDs and use the same timestamp/seq values.
    for run_id, question, answer in (("z-first", "first", "answer one"),
                                     ("a-second", "second", "answer two")):
        store.append_presentation_events([
            {"event_id": f"{run_id}-{seq}", "session_id": "s", "run_id": run_id,
             "segment_id": "seg", "seq": seq, "type": kind, "payload": payload}
            for seq, (kind, payload) in enumerate([
                ("user_message", {"text": question}),
                ("delta", {"text": answer}),
                ("done", {"steps": 1, "completed": True}),
            ], 1)
        ])
    turns = build_presentation(store.read_presentation_events("s"))["turns"]
    assert [(t["user_text"], t["blocks"]) for t in turns] == [
        ("first", [{"type": "text", "text": "answer one"}]),
        ("second", [{"type": "text", "text": "answer two"}]),
    ]
    assert all(t["terminal"] for t in turns)


# ---- SSE 编码 ----


def test_encode_reasoning_delta_and_display() -> None:
    from erpilot_api.events import encode_event

    assert encode_event(ReasoningDelta(step=1, text="思考片段")) == (
        "reasoning_delta", {"step": 1, "text": "思考片段"},
    )
    display = {"version": 1, "kind": "stock_query", "outcome": "succeeded", "quantity": 18}
    finished = ToolCallFinished(
        call_id="c1", name="check_stock", content="{}", ok=True, display=display,
    )
    name, payload = encode_event(finished)
    assert name == "tool_finished" and payload["display"] == display


def test_encode_tool_finished_without_display_keeps_null() -> None:
    from erpilot_api.events import encode_event

    _, payload = encode_event(
        ToolCallFinished(call_id="c1", name="echo", content="{}", ok=True)
    )
    assert payload["display"] is None
