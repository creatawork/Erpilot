import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import {
  cancelSession,
  submitApproval,
  loadSessionState,
  streamApprovalResume,
  streamInterruptedResume,
  streamRunEvents,
} from "../.test-build/protocol.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("approval decision sends the pending id and boolean", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/chat/approve");
    assert.deepEqual(JSON.parse(options.body), { pending_id: "p1", approved: false });
    return Response.json({ ok: true });
  };
  await submitApproval("p1", false);
});

test("an expired decision is shown as a failure despite HTTP 200", async () => {
  globalThis.fetch = async () => Response.json({ ok: false });
  await assert.rejects(submitApproval("old", true), error =>
    error.status === 409 && error.code === "stale_approval" && /已失效/.test(error.message));
});

test("HTTP and transport failures are propagated for retry", async () => {
  globalThis.fetch = async () => new Response(null, { status: 503 });
  await assert.rejects(submitApproval("p1", true), error => error.status === 503 && /503/.test(error.message));
  globalThis.fetch = async () => { throw new Error("offline"); };
  await assert.rejects(submitApproval("p1", true), /offline/);
});

test("snapshot hydrates messages and fixed approval payloads", async () => {
  const snapshot = { session_id: "s1", status: "waiting_approval",
    messages: [{role: "user", content: "write"}], pending_approvals: [{
      call_id: "c1", pending_id: "p1", tool: "write", risk: "confirm", arguments: {sku: "A1"},
    }], tool_results: [] };
  globalThis.fetch = async url => {
    assert.equal(url, "/api/sessions/s1/state");
    return Response.json(snapshot);
  };
  assert.deepEqual(await loadSessionState("s1"), snapshot);
});

test("only a newly-created session may treat 404 as an empty snapshot", async () => {
  globalThis.fetch = async () => Response.json({error: {code: "session_not_found", message: "missing"}}, {status: 404});
  assert.equal(await loadSessionState("new", undefined, true), null);
  await assert.rejects(loadSessionState("existing"), /missing/);
  globalThis.fetch = async () => Response.json({error: {code: "db_error", message: "unavailable"}}, {status: 503});
  await assert.rejects(loadSessionState("s1"), /unavailable/);
});

test("resume sends only decision fields and consumes the existing SSE union", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/chat/approve/stream");
    assert.deepEqual(JSON.parse(options.body), {
      session_id: "s1", pending_id: "p1", approved: false, reason: "no",
    });
    return new Response('event: approval_resolved\r\ndata: {"pending_id":"p1","approved":false}\r\n\r\nevent: error\ndata: {"code":"stale_approval","message":"stale"}\n\n');
  };
  const events = [];
  for await (const event of streamApprovalResume("s1", "p1", false, "no")) events.push(event);
  assert.deepEqual(events.map(e => e.event), ["approval_resolved", "error"]);
  assert.equal(events[1].data.code, "stale_approval");
});

test("resume rejects a stale approval before parsing SSE", async () => {
  globalThis.fetch = async () => Response.json({error: {code: "stale_approval", message: "已失效"}}, {status: 409});
  await assert.rejects(async () => {
    for await (const _event of streamApprovalResume("s1", "old", true)) { /* drain */ }
  }, /已失效/);
});

test("interrupted session continuation uses its session resume stream", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/sessions/s1/resume/stream");
    assert.equal(options.method, "POST");
    return new Response('event: done\ndata: {"completed":true}\n\n');
  };
  const events = [];
  for await (const event of streamInterruptedResume("s1")) events.push(event);
  assert.deepEqual(events.map(event => event.event), ["done"]);
});

test("run event replay parses the durable sequence cursor and run identity", async () => {
  globalThis.fetch = async url => {
    assert.equal(url, "/api/runs/r1/events?after_seq=4");
    return new Response('id: 5\nevent: reconciliation_pending\ndata: {"run_id":"r1","call_id":"c1","client_token":"t","code":"unknown","message":"check"}\n\n');
  };
  const events = [];
  for await (const event of streamRunEvents("r1", 4)) events.push(event);
  assert.equal(events[0].seq, 5);
  assert.equal(events[0].run_id, "r1");
  assert.equal(events[0].event, "reconciliation_pending");
});

test("cancel endpoint returns the refreshed session snapshot", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/sessions/s1/cancel");
    assert.equal(options.method, "POST");
    return Response.json({session_id: "s1", status: "completed", messages: [],
      pending_approvals: [], tool_results: []});
  };
  assert.equal((await cancelSession("s1")).status, "completed");
});

import { streamChat, ProtocolError } from "../.test-build/protocol.js";

function sseResponse(chunks) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: {"content-type": "text/event-stream"} });
}

async function collect(makeResponse) {
  globalThis.fetch = async () => makeResponse();
  const events = [];
  for await (const ev of streamChat("hi", "s1")) events.push(ev);
  return events;
}

test("tool_executing events are parsed and forwarded", async () => {
  const events = await collect(() => sseResponse([
    'event: tool_started\ndata: {"id":"c1","name":"check_stock","arguments":"{}"}\n\n',
    'event: tool_executing\ndata: {"id":"c1","name":"check_stock"}\n\n',
    'event: tool_finished\ndata: {"id":"c1","name":"check_stock","content":"{}","ok":true}\n\n',
  ]));
  assert.deepEqual(events.map(e => e.event),
    ["tool_started", "tool_executing", "tool_finished"]);
});

test("network fragmentation and UTF-8 splits never corrupt events", async () => {
  const frame = 'event: delta\ndata: {"text":"库存充足，价格 ¥9.9"}\n\n';
  const bytes = new TextEncoder().encode(frame.repeat(2));
  const events = await collect(() => {
    const stream = new ReadableStream({
      start(controller) {
        // 在任意字节边界切分，覆盖多字节汉字跨片段
        for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
        controller.close();
      },
    });
    return new Response(stream, { status: 200, headers: {"content-type": "text/event-stream"} });
  });
  assert.equal(events.length, 2);
  assert.equal(events[0].data.text, "库存充足，价格 ¥9.9");
  assert.equal(events[1].data.text, "库存充足，价格 ¥9.9");
});

test("multi-line data joins per the SSE spec; comment heartbeats are ignored", async () => {
  const events = await collect(() => sseResponse([
    ': ping\n\n',
    ': keep-alive\n\n',
    'event: delta\ndata: {\ndata: "text": "分段回答"\ndata: }\n\n',
  ]));
  assert.equal(events.length, 1);
  assert.equal(events[0].data.text, "分段回答");
});

test("unknown events are ignored with a diagnostic instead of crashing the page", async () => {
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = msg => warnings.push(String(msg));
  try {
    const events = await collect(() => sseResponse([
      'event: future_feature\ndata: {"whatever":true}\n\n',
      'event: done\ndata: {"completed":true}\n\n',
    ]));
    assert.deepEqual(events.map(e => e.event), ["done"]);
  } finally {
    console.warn = originalWarn;
  }
  assert.ok(warnings.some(w => w.includes("future_feature")));
});

test("invalid JSON on a known event becomes an explainable protocol error", async () => {
  await assert.rejects(
    collect(() => sseResponse(['event: delta\ndata: {broken}\n\n'])),
    error => error instanceof ProtocolError && error.eventName === "delta" && /delta/.test(error.message),
  );
});

test("heartbeat and reasoning_delta are recognized events; reasoning carries the step", async () => {
  const events = await collect(() => sseResponse([
    'event: heartbeat\ndata: {"at":"2026-10-07T02:00:00Z"}\n\n',
    'event: reasoning_delta\ndata: {"step":1,"text":"思考片段"}\n\n',
  ]));
  assert.deepEqual(events.map(e => e.event), ["heartbeat", "reasoning_delta"]);
  assert.equal(events[1].data.step, 1);
});
