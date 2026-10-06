import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { submitApproval, loadSessionState, streamApprovalResume, streamInterruptedResume } from "../.test-build/protocol.js";

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
  await assert.rejects(submitApproval("old", true), /已失效/);
});

test("HTTP and transport failures are propagated for retry", async () => {
  globalThis.fetch = async () => new Response(null, { status: 503 });
  await assert.rejects(submitApproval("p1", true), /503/);
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

test("new sessions return no snapshot and stale sessions propagate structured errors", async () => {
  globalThis.fetch = async () => Response.json({error: {code: "session_not_found", message: "missing"}}, {status: 404});
  assert.equal(await loadSessionState("new"), null);
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
