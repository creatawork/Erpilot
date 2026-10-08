import assert from "node:assert/strict";
import { test } from "node:test";
import {
  applyTurnEvent,
  acceptEventCursor,
  canSendMessage,
  hydrateTurns,
  isTerminalPhase,
  markDisconnected,
  newAssistantTurn,
  phaseLabel,
  toolSummary,
  toolTitle,
} from "../.test-build/session.js";

test("message sending stays blocked until recovery succeeds and while server reports unfinished work", () => {
  assert.equal(canSendMessage("loading", null, 0, false), false);
  assert.equal(canSendMessage("recovery_failed", null, 0, false), false);
  assert.equal(canSendMessage("ready", "waiting_approval", 0, false), false);
  assert.equal(canSendMessage("ready", "reconciliation_required", 0, false), false);
  assert.equal(canSendMessage("ready", "running", 0, false), false);
  assert.equal(canSendMessage("ready", "interrupted", 0, false), false);
  assert.equal(canSendMessage("ready", "completed", 1, false), false);
  assert.equal(canSendMessage("new", null, 0, false), true);
  assert.equal(canSendMessage("ready", "completed", 0, false), true);
});

test("event replay de-duplicates by run id and sequence while leaving live events alone", () => {
  const cursor = new Map([['r1', 5]]);
  assert.equal(acceptEventCursor({event: "delta", data: {text: "old"}, run_id: "r1", seq: 5}, cursor), false);
  assert.equal(acceptEventCursor({event: "delta", data: {text: "new"}, run_id: "r1", seq: 6}, cursor), true);
  assert.equal(cursor.get("r1"), 6);
  assert.equal(acceptEventCursor({event: "delta", data: {text: "live"}}, cursor), true);
});

test("unknown recovery snapshot stays pending and is not presented as failure or success", () => {
  const turns = hydrateTurns({session_id: "s", status: "reconciliation_required", messages: [
    {role: "user", content: "改库存"},
    {role: "assistant", tool_calls: [
      {id: "w", function: {name: "adjust_stock", arguments: '{"sku":"A","delta":1}'}},
    ]},
    {role: "tool", tool_call_id: "w", content: '{"error":{"code":"reconciliation_unavailable"}}'},
  ], pending_approvals: [], pending_reconciliation: {
    call_id: "w", client_token: "stable", code: "reconciliation_unavailable", message: "待核对",
  }, tool_results: [{call_id: "w", name: "adjust_stock", ok: false,
    content: '{"error":{"code":"reconciliation_unavailable"}}',
    invocation_status: "unknown"}],
  });
  assert.equal(turns[1].phase, "reconciliation_required");
  assert.equal(turns[1].tools.w.status, "unknown");
});

test("hydration pairs tool results and restores normalized pending arguments", () => {
  const turns = hydrateTurns({session_id: "s", status: "waiting_approval", messages: [
    {role: "system", content: "system"},
    {role: "user", content: "write"},
    {role: "assistant", content: "checking", tool_calls: [
      {id: "r", function: {name: "check_stock", arguments: "{}"}},
      {id: "w", function: {name: "adjust_stock", arguments: '{"sku":"A"}'}},
    ]},
    {role: "tool", tool_call_id: "r", content: "result"},
  ], tool_results: [{call_id: "r", name: "check_stock", content: "failed result", ok: false}],
  pending_approvals: [{call_id: "w", pending_id: "p", tool: "adjust_stock", risk: "confirm",
    arguments: {sku: "A", client_token: "stable"}}]});
  assert.equal(turns.length, 2);
  const assistant = turns[1];
  assert.equal(assistant.blocks.length, 3);
  assert.deepEqual(assistant.blocks[0], {id: assistant.blocks[0].id, type: "text", step: 0, text: "checking"});
  assert.deepEqual(assistant.blocks[1], {id: assistant.blocks[1].id, type: "tool", callId: "r"});
  assert.deepEqual(assistant.blocks[2], {id: assistant.blocks[2].id, type: "tool", callId: "w"});
  const read = assistant.tools.r;
  assert.equal(read.status, "failed");
  assert.equal(read.finished.content, "failed result");
  const write = assistant.tools.w;
  assert.equal(write.status, "waiting_approval");
  assert.equal(write.approval.pending_id, "p");
  assert.deepEqual(JSON.parse(write.arguments), {sku: "A", client_token: "stable"});
});

test("hydration marks denied results and never shows running cursors on finished history", () => {
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "write"},
    {role: "assistant", tool_calls: [{id: "w", function: {name: "create_order", arguments: "{}"}}]},
  ], tool_results: [{
    call_id: "w", name: "create_order", ok: true,
    content: JSON.stringify({approval: "denied", message: "操作未执行：额度不足"}),
  }], pending_approvals: []});
  const assistant = turns[1];
  assert.equal(assistant.tools.w.status, "denied");
  assert.ok(isTerminalPhase(assistant.phase));
  assert.equal(assistant.phase, "completed");
});

test("a send creates a connecting placeholder before any server event arrives", () => {
  const turn = newAssistantTurn(1000);
  assert.equal(turn.phase, "connecting");
  assert.equal(phaseLabel(turn), "正在连接…");
  assert.equal(turn.blocks.length, 0);
});

test("deltas merge into one text block per step and split after tool boundaries", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "start", data: {session_id: "s", model: "m"}}, 1100);
  turn = applyTurnEvent(turn, {event: "step", data: {step: 1}}, 1200);
  turn = applyTurnEvent(turn, {event: "delta", data: {text: "先查一下"}}, 1300);
  turn = applyTurnEvent(turn, {event: "delta", data: {text: "库存"}}, 1400);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "c1", name: "check_stock", arguments: "{}"}}, 1500);
  turn = applyTurnEvent(turn, {event: "delta", data: {text: "查到了"}}, 1600);

  assert.equal(turn.phase, "generating");
  assert.equal(phaseLabel(turn), "正在生成回复");
  assert.equal(turn.blocks.length, 3);
  assert.deepEqual(turn.blocks.map(b => b.type), ["text", "tool", "text"]);
  assert.equal(turn.blocks[0].text, "先查一下库存");
  assert.equal(turn.blocks[2].text, "查到了");
  assert.deepEqual(Object.keys(turn.tools), ["c1"]);
  assert.equal(turn.tools.c1.status, "prepared");
});

test("tool_executing marks running only after preparation and before results", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "c1", name: "check_stock", arguments: "{}"}}, 1100);
  turn = applyTurnEvent(turn, {event: "tool_executing", data: {id: "c1", name: "check_stock"}}, 1200);
  assert.equal(turn.tools.c1.status, "running");
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "c1", name: "check_stock", content: '{"quantity":9}', ok: true}}, 1300);
  assert.equal(turn.tools.c1.status, "succeeded");
});

test("parallel tools keep request order while finishing out of order", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "a", name: "check_stock", arguments: '{"sku":"A"}'}}, 1100);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "b", name: "get_price", arguments: '{"sku":"A"}'}}, 1200);
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "b", name: "get_price", content: "9", ok: true}}, 1300);
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "a", name: "check_stock", content: '{"stock":9}', ok: true}}, 1400);
  assert.deepEqual(turn.blocks.map(b => b.callId ?? b.type), ["a", "b"]);
  assert.equal(turn.tools.a.status, "succeeded");
  assert.equal(turn.tools.b.status, "succeeded");
});

test("a denied call never flips to business success even when the backfill returns ok", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "w", name: "adjust_stock", arguments: '{"sku":"A1001","delta":9}'}}, 1100);
  turn = applyTurnEvent(turn, {event: "approval_pending", data: {
    call_id: "w", pending_id: "p", tool: "adjust_stock", risk: "confirm", arguments: {sku: "A1001", delta: 9},
  }}, 1200);
  assert.equal(turn.phase, "awaiting_approval");
  assert.equal(turn.tools.w.status, "waiting_approval");
  turn = applyTurnEvent(turn, {event: "approval_resolved", data: {
    call_id: "w", pending_id: "p", tool: "adjust_stock", approved: false, reason: "额度不足",
  }}, 1300);
  assert.equal(turn.tools.w.status, "denied");
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {
    id: "w", name: "adjust_stock",
    content: JSON.stringify({approval: "denied"}), ok: true,
  }}, 1400);
  assert.equal(turn.tools.w.status, "denied");
  assert.notEqual(turn.phase, "completed");
});

test("approved flow distinguishes waiting-for-execution from actually running", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "w", name: "adjust_stock", arguments: "{}"}}, 1100);
  turn = applyTurnEvent(turn, {event: "approval_pending", data: {
    call_id: "w", pending_id: "p", tool: "adjust_stock", risk: "confirm", arguments: {},
  }}, 1200);
  turn = applyTurnEvent(turn, {event: "approval_resolved", data: {
    call_id: "w", pending_id: "p", tool: "adjust_stock", approved: true, reason: "",
  }}, 1300);
  assert.equal(turn.tools.w.status, "prepared");
  assert.equal(turn.phase, "approved");
  assert.equal(phaseLabel(turn), "已批准，等待执行");
  turn = applyTurnEvent(turn, {event: "tool_executing", data: {id: "w", name: "adjust_stock"}}, 1400);
  assert.equal(turn.tools.w.status, "running");
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "w", name: "adjust_stock", content: "{}", ok: false}}, 1500);
  assert.equal(turn.tools.w.status, "failed");
});

test("done separates normal completion from the max_steps guard; errors and EOF keep received content", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "delta", data: {text: "部分回答"}}, 1100);
  turn = applyTurnEvent(turn, {event: "done", data: {steps: 2, completed: false, usage: null, cost: null, duration_ms: 1, trace: null}}, 1200);
  assert.equal(turn.phase, "max_steps");
  assert.equal(phaseLabel(turn), "达到执行轮次上限");

  let errored = newAssistantTurn(1000);
  errored = applyTurnEvent(errored, {event: "delta", data: {text: "部分回答"}}, 1100);
  errored = applyTurnEvent(errored, {event: "error", data: {code: "execution_error", message: "boom"}}, 1200);
  assert.equal(errored.phase, "failed");

  let dropped = newAssistantTurn(1000);
  dropped = applyTurnEvent(dropped, {event: "delta", data: {text: "部分回答"}}, 1100);
  dropped = markDisconnected(dropped, 1200);
  assert.equal(dropped.phase, "disconnected");
  assert.equal(dropped.blocks[0].text, "部分回答");

  let doneTurn = newAssistantTurn(1000);
  doneTurn = applyTurnEvent(doneTurn, {event: "delta", data: {text: "完整"}}, 1100);
  doneTurn = applyTurnEvent(doneTurn, {event: "done", data: {steps: 1, completed: true, usage: null, cost: null, duration_ms: 1, trace: null}}, 1200);
  assert.equal(markDisconnected(doneTurn, 1300).phase, "completed");
});

test("reducers never mutate the previous turn", () => {
  const base = newAssistantTurn(1000);
  const next = applyTurnEvent(base, {event: "delta", data: {text: "x"}}, 1100);
  assert.equal(base.blocks.length, 0);
  assert.equal(next.blocks.length, 1);
});

test("known tool titles and verified argument summaries", () => {
  assert.equal(toolTitle("check_stock"), "查询库存");
  assert.equal(toolTitle("adjust_stock"), "调整库存");
  assert.equal(toolTitle("mystery_tool"), "mystery_tool");
  const entity = {
    id: "w", name: "adjust_stock", status: "running",
    arguments: '{"sku": "A1001", "delta": 9}',
    finished: null, approval: null, approvalResolved: null,
  };
  assert.equal(toolSummary(entity), "A1001 · +9 件");
  assert.equal(toolSummary({...entity, arguments: "not json"}), "");
});

test("reasoning deltas become separate blocks and never merge into answer text", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "step", data: {step: 1}}, 1100);
  turn = applyTurnEvent(turn, {event: "reasoning_delta", data: {step: 1, text: "先想想"}}, 1200);
  turn = applyTurnEvent(turn, {event: "reasoning_delta", data: {step: 1, text: "价格"}}, 1300);
  turn = applyTurnEvent(turn, {event: "delta", data: {text: "回答"}}, 1400);
  turn = applyTurnEvent(turn, {event: "reasoning_delta", data: {step: 2, text: "下一轮"}}, 1500);
  assert.deepEqual(turn.blocks.map(b => b.type), ["reasoning", "text", "reasoning"]);
  assert.equal(turn.blocks[0].text, "先想想价格");
  assert.equal(turn.blocks[1].text, "回答");
  assert.equal(turn.blocks[2].step, 2);
});

test("heartbeat events never touch turn content", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "heartbeat", data: {at: "2026-10-07T02:00:00Z"}}, 1100);
  assert.equal(turn.blocks.length, 0);
  assert.equal(turn.phase, "connecting");
});

test("tool_finished stores the business display payload", () => {
  let turn = newAssistantTurn(1000);
  turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "c", name: "check_stock", arguments: "{}"}}, 1100);
  turn = applyTurnEvent(turn, {event: "tool_finished", data: {
    id: "c", name: "check_stock", content: "{}", ok: true,
    display: {version: 1, kind: "stock_query", outcome: "succeeded", quantity: 18},
  }}, 1200);
  assert.deepEqual(turn.tools.c.display, {version: 1, kind: "stock_query", outcome: "succeeded", quantity: 18});
});

test("hydration prefers the presentation projection and falls back to messages", () => {
  const turns = hydrateTurns({
    session_id: "s", status: "completed",
    presentation: {
      version: 1, last_seq: 9, updated_at: "2026-10-07 10:00:00",
      turns: [{
        run_id: "r1", user_text: "查库存",
        blocks: [{type: "text", text: "库存 18 件"}, {type: "tool", callId: "c1"}],
        tools: {c1: {
          name: "check_stock", arguments: '{"sku":"A1001"}', status: "succeeded",
          finished: {id: "c1", name: "check_stock", content: "{}", ok: true,
            display: {version: 1, kind: "stock_query", outcome: "succeeded", quantity: 18}},
          approval: null, approvalResolved: null,
        }},
        done: {steps: 2, completed: true, usage: null, cost: null, duration_ms: 1, trace: null},
        error: null, terminal: true,
      }],
    },
    messages: [{role: "user", content: "查库存"}, {role: "assistant", content: "库存 18 件",
      tool_calls: [{id: "c1", function: {name: "check_stock", arguments: '{"sku":"A1001"}'}}]},
      {role: "tool", tool_call_id: "c1", content: "{}"}],
    pending_approvals: [], tool_results: [],
  });
  assert.equal(turns[0].text, "查库存");
  const assistant = turns[1];
  assert.equal(assistant.phase, "completed");
  assert.deepEqual(assistant.blocks.map(b => b.type), ["text", "tool"]);
  assert.equal(assistant.tools.c1.display.kind, "stock_query");
});

test("an unfinished projection run is marked as partially unsaved, not completed", () => {
  const turns = hydrateTurns({
    session_id: "s", status: "completed",
    presentation: {
      version: 1, last_seq: 3, updated_at: null,
      turns: [{
        run_id: "r1", user_text: "hi",
        blocks: [{type: "text", text: "写到一半"}],
        tools: {}, done: null, error: null, terminal: false,
      }],
    },
    messages: [], pending_approvals: [], tool_results: [],
  });
  const assistant = turns[1];
  assert.equal(assistant.phase, "disconnected");
  assert.match(assistant.error, /部分输出未保存/);
});

test("a waiting-approval tool in the projection restores the approval card", () => {
  const turns = hydrateTurns({
    session_id: "s", status: "waiting_approval",
    presentation: {
      version: 1, last_seq: 5, updated_at: null,
      turns: [{
        run_id: "r1", user_text: "改库存",
        blocks: [{type: "tool", callId: "w"}],
        tools: {w: {
          name: "adjust_stock", arguments: '{"sku":"A1001"}', status: "waiting_approval",
          finished: null, display: null,
          approval: {call_id: "w", pending_id: "p1", tool: "adjust_stock", risk: "confirm",
            arguments: {sku: "A1001", delta: 9}},
          approvalResolved: null,
        }},
        done: null, error: null, terminal: false,
      }],
    },
    messages: [], pending_approvals: [], tool_results: [],
  });
  const assistant = turns[1];
  assert.equal(assistant.phase, "awaiting_approval");
  assert.equal(assistant.tools.w.approval.pending_id, "p1");
});

test("a partial projection preserves older messages including repeated questions", () => {
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "查库存"}, {role: "assistant", content: "旧库存"},
    {role: "user", content: "查库存"}, {role: "assistant", content: "新库存"},
  ], pending_approvals: [], tool_results: [], presentation: {
    version: 1, last_seq: 4, updated_at: null, turns: [{
      run_id: "new", user_index: 1, user_text: "查库存", blocks: [{type: "text", text: "新库存"}],
      tools: {}, done: null, error: null, terminal: true,
    }],
  }});
  assert.equal(turns.length, 4);
  assert.equal(turns[1].blocks[0].text, "旧库存");
  assert.equal(turns[3].blocks[0].text, "新库存");
});

test("completed checkpoint replaces stale failure output after retry", () => {
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "恢复"}, {role: "assistant", content: "恢复后的完整回答"},
  ], pending_approvals: [], tool_results: [], presentation: {
    version: 1, last_seq: 4, updated_at: null, turns: [{
      run_id: "r", user_text: "恢复", blocks: [{type: "text", text: "未完成"}],
      tools: {}, done: null, error: "old error", terminal: true,
    }],
  }});
  assert.equal(turns[1].phase, "completed");
  assert.equal(turns[1].error, null);
  assert.equal(turns[1].blocks[0].text, "恢复后的完整回答");
});

test("completed checkpoint clears a stale pending approval without losing tool results", () => {
  const pending = {call_id: "w", pending_id: "old", tool: "adjust_stock", risk: "confirm",
    arguments: {sku: "A1001", delta: 9}};
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "改库存"},
    {role: "assistant", tool_calls: [{id: "w", function: {name: "adjust_stock", arguments: "{}"}}]},
    {role: "tool", tool_call_id: "w", content: '{"sku":"A1001","quantity":9}'},
    {role: "assistant", content: "已完成"},
  ], pending_approvals: [], tool_results: [{call_id: "w", name: "adjust_stock", ok: true,
    content: '{"sku":"A1001","quantity":9}'}], presentation: {
    version: 1, last_seq: 5, updated_at: null, turns: [{
      run_id: "r", user_text: "改库存", blocks: [{type: "tool", callId: "w"}],
      tools: {w: {name: "adjust_stock", arguments: "{}", status: "waiting_approval",
        finished: null, approval: pending, approvalResolved: null}},
      done: null, error: "GeneratorExit", terminal: true,
    }],
  }});
  assert.equal(turns.length, 2);
  assert.equal(turns[1].phase, "completed");
  assert.equal(turns[1].tools.w.status, "succeeded");
  assert.equal(turns[1].tools.w.approval, null);
  assert.equal(turns[1].blocks.at(-1).text, "已完成");
});

test("legacy hydration groups model steps and recovers results from tool messages", () => {
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "查询"},
    {role: "assistant", content: "先查", tool_calls: [
      {id: "r", function: {name: "get_stock", arguments: '{"sku":"A"}'}},
    ]},
    {role: "tool", tool_call_id: "r", content: '{"quantity":9}'},
    {role: "assistant", content: "查好了"},
  ], pending_approvals: [], tool_results: []});
  assert.equal(turns.length, 2);
  assert.deepEqual(turns[1].blocks.map(b => b.type), ["text", "tool", "text"]);
  assert.equal(turns[1].tools.r.finished.content, '{"quantity":9}');
});

test("an active checkpoint retains streamed projection text without claiming disconnection", () => {
  const turns = hydrateTurns({session_id: "s", status: "running", messages: [
    {role: "user", content: "查询"},
  ], pending_approvals: [], tool_results: [], presentation: {
    version: 1, last_seq: 3, updated_at: null, turns: [{
      run_id: "r", user_text: "查询", blocks: [{type: "text", text: "流式片段"}],
      tools: {}, done: null, error: null, terminal: false,
    }],
  }});
  assert.equal(turns[1].blocks[0].text, "流式片段");
  assert.equal(turns[1].phase, "generating");
  assert.equal(turns[1].error, null);
});

test("tool summaries tolerate null, arrays, scalars and malformed JSON", () => {
  for (const args of ["null", "[]", "9", '"text"', "not json"]) {
    assert.equal(toolSummary({arguments: args}), "");
  }
});

test("business failures and unknown outcomes are never transport successes", () => {
  for (const outcome of ["failed", "unknown"]) {
    let turn = newAssistantTurn();
    turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "w", name: "adjust_stock",
      arguments: '{"sku":"X","delta":1}'}});
    turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "w", name: "adjust_stock",
      content: "{}", ok: true, display: {version: 1, kind: "stock_adjustment", outcome}}});
    assert.equal(turn.tools.w.status, outcome);
  }
});

test("legacy business errors are classified from content without display", () => {
  const turns = hydrateTurns({session_id: "s", status: "completed", messages: [
    {role: "user", content: "改库存"}, {role: "assistant", tool_calls: [
      {id: "w", function: {name: "adjust_stock", arguments: '{"sku":"X","delta":1}'}},
    ]}, {role: "tool", tool_call_id: "w", content: '{"error":{"code":"not_found"}}'},
  ], pending_approvals: [], tool_results: []});
  assert.equal(turns[1].tools.w.status, "failed");
});

test("known business tools with unconfirmed results stay unknown without display", () => {
  for (const name of ["get_stock", "check_stock", "adjust_stock", "create_order"]) {
    let turn = newAssistantTurn();
    turn = applyTurnEvent(turn, {event: "tool_started", data: {id: "w", name, arguments: "{}"}});
    turn = applyTurnEvent(turn, {event: "tool_finished", data: {id: "w", name, content: "{}", ok: true}});
    assert.equal(turn.tools.w.status, "unknown");
  }
});

test("an interrupted checkpoint preserves committed tools when the journal lags", () => {
  const turns = hydrateTurns({session_id: "s", status: "interrupted", messages: [
    {role: "user", content: "查询"}, {role: "assistant", content: "先查", tool_calls: [
      {id: "r", function: {name: "get_stock", arguments: "{}"}},
    ]}, {role: "tool", tool_call_id: "r", content: '{"quantity":9}'},
  ], pending_approvals: [], tool_results: [], presentation: {
    version: 1, last_seq: 2, updated_at: null, turns: [{
      run_id: "r", user_text: "查询", blocks: [{type: "text", text: "先"}],
      tools: {}, done: null, error: null, terminal: false,
    }],
  }});
  assert.deepEqual(turns[1].blocks.map(b => b.type), ["text", "tool"]);
  assert.equal(turns[1].tools.r.status, "succeeded");
  assert.equal(turns[1].phase, "disconnected");
});
