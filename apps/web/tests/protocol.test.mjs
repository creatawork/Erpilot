import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { submitApproval } from "../.test-build/protocol.js";
import * as protocol from "../.test-build/protocol.js";

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

const pending = { pending_id: "p1", invocation_id: "i1", status: "pending", expected_version: 1,
  arguments_fingerprint: "fp1", expires_at: "2099-01-01T00:00:00+00:00" };
function snapshot(overrides = {}) {
  return { run_id: "r1", session_id: "s1", user_input: "调整库存", status: "waiting_approval",
    messages: [], answer_text: "已查库存", final_answer: null, error: null, trace_path: "trace.json",
    invocations: [{ invocation_id: "i1", call_id: "c1", tool: "adjust_stock", arguments: { sku: "A", delta: 1 },
      status: "waiting_approval", result: null, approval_pending_id: "p1" }], approvals: [pending],
    pending: [pending], last_seq: 3,
    events: [{ event: "delta", data: { run_id: "r1", seq: 3, text: "已查库存" } }], ...overrides };
}

test("snapshot restores the original approval identity without duplicating answer replay", () => {
  const run = protocol.restoreRun(snapshot());
  assert.equal(run.text, "已查库存");
  assert.equal(run.lastSeq, 3);
  assert.equal(run.tools.length, 1);
  assert.equal(run.tools[0].approval.arguments_fingerprint, "fp1");
  assert.equal(run.tools[0].approval.expected_version, 1);
  assert.equal(run.tools[0].approval.expires_at, pending.expires_at);
});

test("run/sequence replay and duplicate tool starts never append a second answer or card", () => {
  let run = protocol.restoreRun(snapshot());
  const event = { event: "delta", data: { run_id: "r1", seq: 4, text: "，待批准" } };
  run = protocol.applyRunEvent(run, event);
  run = protocol.applyRunEvent(run, event);
  run = protocol.applyRunEvent(run, { event: "delta", data: { run_id: "other", seq: 99, text: "错误" } });
  run = protocol.applyRunEvent(run, { event: "tool_started", data: { run_id: "r1", seq: 5,
    id: "c1", name: "adjust_stock", arguments: "{}" } });
  assert.equal(run.text, "已查库存，待批准");
  assert.equal(run.lastSeq, 5);
  assert.equal(run.tools.length, 1);
  assert.equal(run.tools[0].approval.pending_id, "p1");
});

test("unknown outcome and committed business with unfinished answer are distinct", () => {
  const unknown = snapshot({ status: "recovering" });
  unknown.invocations[0].status = "unknown";
  assert.match(protocol.runNotice(protocol.restoreRun(unknown)), /结果待核对/);
  const committed = snapshot({ status: "recovering" });
  committed.invocations[0].status = "succeeded";
  committed.invocations[0].result = { quantity: 2 };
  assert.match(protocol.runNotice(protocol.restoreRun(committed)), /业务已执行.*回答/);
  const cancelled = { ...committed, status: "cancelled" };
  assert.match(protocol.runNotice(protocol.restoreRun(cancelled)), /未撤销/);
});

test("expired and resolved cards cannot submit another decision", () => {
  assert.equal(protocol.canDecideApproval(pending, 0), true);
  assert.equal(protocol.canDecideApproval({ ...pending, expires_at: "2000-01-01T00:00:00Z" }, Date.now()), false);
  assert.equal(protocol.canDecideApproval({ ...pending, status: "approved" }, 0), false);
});

test("bound approval sends ownership, fingerprint and version", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/chat/approve");
    assert.deepEqual(JSON.parse(options.body), { pending_id: "p1", approved: true, run_id: "r1",
      session_id: "s1", expected_version: 1, arguments_fingerprint: "fp1" });
    return Response.json({ ok: true });
  };
  await submitApproval("p1", true, { run_id: "r1", session_id: "s1", expected_version: 1, arguments_fingerprint: "fp1" });
});

test("stale approval preserves structured conflict information for state refresh", async () => {
  globalThis.fetch = async () => Response.json({ detail: { message: "审批已拒绝", approval: { ...pending, status: "denied" } } }, { status: 409 });
  await assert.rejects(submitApproval("p1", true), error => error.status === 409 && error.message === "审批已拒绝");
});

test("recovery resumes the existing run and subscribes after its atomic snapshot cursor", async () => {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push([url, options.method || "GET"]);
    if (url === "/api/runs/r1/events?after_seq=3") {
      return new Response('event: delta\r\ndata: {"run_id":"r1","seq":4,"text":"，完成"}\r\n\r\n');
    }
    return Response.json(snapshot(requests.length === 4 ? { status: "completed", last_seq: 4, final_answer: "最终回答", answer_text: "最终回答" } : {}));
  };
  const updates = [];
  for await (const run of protocol.recoverRun("r1")) updates.push(run);
  assert.deepEqual(requests, [["/api/runs/r1", "GET"], ["/api/runs/r1/resume", "POST"],
    ["/api/runs/r1/events?after_seq=3", "GET"], ["/api/runs/r1", "GET"]]);
  assert.equal(updates.at(-1).text, "最终回答");
  assert.equal(updates.at(-1).status, "completed");
});

test("completed session history restores without any message POST or resume", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/sessions/s1/runs");
    assert.equal(options?.method, undefined);
    return Response.json({ session_id: "s1", runs: [snapshot({ status: "completed", final_answer: "报价 10 元", answer_text: "报价 10 元" })] });
  };
  const runs = await protocol.loadSessionRuns("s1");
  assert.equal(runs.length, 1);
  assert.equal(runs[0].text, "报价 10 元");
});

test("transient recovery disconnect backs off before reconnecting and never starts another run", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push([url, options.method || "GET"]);
    if (requests.length === 1) throw new Error("offline");
    return Response.json(snapshot({ status: "completed", answer_text: "恢复完成" }));
  };
  const updates = protocol.followRun("r1");
  const first = await updates.next();
  assert.equal(first.value.kind, "disconnected");
  const reconnect = updates.next();
  await Promise.resolve();
  assert.equal(requests.length, 1);
  context.mock.timers.tick(999);
  await Promise.resolve();
  assert.equal(requests.length, 1);
  context.mock.timers.tick(1);
  assert.equal((await reconnect).value.run.text, "恢复完成");
  assert.equal((await updates.next()).done, true);
  assert.deepEqual(requests, [["/api/runs/r1", "GET"], ["/api/runs/r1", "GET"]]);
});

test("unknown recovery closes for manual verification instead of looping on business writes", async () => {
  const unknown = snapshot({ status: "recovering" });
  unknown.invocations[0].status = "unknown";
  let calls = 0;
  globalThis.fetch = async url => {
    calls++;
    return url.includes("events") ? new Response("") : Response.json(unknown);
  };
  const updates = [];
  for await (const update of protocol.followRun("r1")) updates.push(update);
  assert.equal(calls, 4);
  assert.equal(updates.at(-1).run.tools[0].status, "unknown");
});

test("cancel calls only the existing run cancellation endpoint and preserves committed results", async () => {
  const committed = snapshot({ status: "cancelled" });
  committed.invocations[0].status = "succeeded";
  committed.invocations[0].result = { quantity: 2 };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/runs/r1/cancel");
    assert.equal(options.method, "POST");
    return Response.json(committed);
  };
  const run = await protocol.cancelRun("r1");
  assert.equal(run.tools[0].finished.ok, true);
  assert.match(protocol.runNotice(run), /未撤销/);
});

test("new run creation posts the message once and uses the returned durable identity", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/runs");
    assert.deepEqual(JSON.parse(options.body), { message: "调整库存", session_id: "s1" });
    return Response.json(snapshot());
  };
  const run = await protocol.createRun("调整库存", "s1");
  assert.equal(run.runId, "r1");
});

test("checkpoint history tool calls stay attached to their original turn", () => {
  const run = protocol.restoreRun(snapshot({ messages: [
    { role: "user", content: "前一轮报价" },
    { role: "assistant", tool_calls: [{ id: "old", function: { name: "quote", arguments: "{}" } }] },
    { role: "tool", tool_call_id: "old", content: "报价 10 元" },
    { role: "user", content: "调整库存" },
    { role: "assistant", tool_calls: [{ id: "c1", function: { name: "adjust_stock", arguments: "{}" } }] },
  ] }));
  assert.equal(run.tools.length, 1);
  assert.equal(run.tools[0].id, "c1");
});

test("terminal recovery does not resume or subscribe again", async () => {
  let calls = 0;
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.equal(url, "/api/runs/r1");
    assert.equal(options?.method, undefined);
    return Response.json(snapshot({ status: "completed", answer_text: "报价完成" }));
  };
  const runs = [];
  for await (const run of protocol.recoverRun("r1")) runs.push(run);
  assert.equal(calls, 1);
  assert.equal(runs[0].text, "报价完成");
});

test("partial tool group keeps completed result while restoring a second pending decision", () => {
  const second = { ...pending, pending_id: "p2", invocation_id: "i2" };
  const group = snapshot();
  group.invocations[0].status = "succeeded";
  group.invocations[0].result = { quantity: 2 };
  group.invocations.push({ ...group.invocations[0], invocation_id: "i2", call_id: "c2",
    approval_pending_id: "p2", status: "waiting_approval", result: null });
  group.approvals = [{ ...pending, status: "approved" }, second];
  const run = protocol.restoreRun(group);
  assert.equal(run.tools.length, 2);
  assert.equal(run.tools[0].finished.ok, true);
  assert.equal(protocol.canDecideApproval(run.tools[0].approval), false);
  assert.equal(protocol.canDecideApproval(run.tools[1].approval), true);
});

test("incompatible recovery conflict remains visible for manual retry without an automatic loop", async () => {
  let calls = 0;
  globalThis.fetch = async url => {
    calls++;
    return url.endsWith("resume") ? Response.json({ detail: "工具版本不兼容" }, { status: 409 }) : Response.json(snapshot());
  };
  const updates = [];
  for await (const update of protocol.followRun("r1")) updates.push(update);
  assert.equal(calls, 2);
  assert.equal(updates.at(-1).retrying, false);
  assert.match(updates.at(-1).message, /不兼容/);
});

test("chunked UTF-8 events and repeated pending events preserve a single actionable card", async () => {
  const content = new TextEncoder().encode('event: approval_pending\ndata: {"run_id":"r1","seq":4,"call_id":"c1","pending_id":"p1","tool":"adjust_stock","risk":"高","arguments":{"sku":"A","delta":1},"expected_version":1,"arguments_fingerprint":"fp1","expires_at":"2099-01-01T00:00:00Z"}\n\nevent: delta\ndata: {"run_id":"r1","seq":5,"text":"，等待批准"}\n\n');
  globalThis.fetch = async url => {
    if (url.includes("events")) return new Response(new ReadableStream({ start(controller) {
      for (let i = 0; i < content.length; i += 7) controller.enqueue(content.slice(i, i + 7));
      controller.close();
    } }));
    return Response.json(snapshot());
  };
  const runs = [];
  for await (const run of protocol.recoverRun("r1")) runs.push(run);
  const streamed = runs.find(run => run.lastSeq === 5);
  assert.equal(streamed.text, "已查库存，等待批准");
  assert.equal(streamed.tools.length, 1);
  assert.equal(streamed.tools[0].approval.risk, "高");
  assert.equal(protocol.canDecideApproval(streamed.tools[0].approval), true);
});

test("aborting reconnect stops subscription work without issuing business cancellation", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const controller = new AbortController();
  const requests = [];
  globalThis.fetch = async url => { requests.push(url); throw new Error("offline"); };
  const updates = protocol.followRun("r1", controller.signal);
  await updates.next();
  const pendingUpdate = updates.next();
  await Promise.resolve();
  controller.abort();
  assert.equal((await pendingUpdate).done, true);
  context.mock.timers.tick(60000);
  assert.deepEqual(requests, ["/api/runs/r1"]);
});

test("cancelled unknown results retain an explicit verification action without resuming writes", async () => {
  const unresolved = snapshot({ status: "cancelled" });
  unresolved.invocations[0].status = "unknown";
  const cancelled = protocol.restoreRun(unresolved);
  assert.equal(protocol.needsResultVerification(cancelled), true);
  assert.equal(protocol.isActiveRun(cancelled.status), false);
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push([url, options?.method ?? "GET"]);
    if (requests.length === 1) return Response.json(unresolved);
    const verified = snapshot({ status: "cancelled", last_seq: 4 });
    verified.invocations[0].status = "succeeded";
    verified.invocations[0].result = { quantity: 2 };
    return Response.json(verified);
  };
  const stillUnknown = await protocol.verifyCancelledRun(cancelled);
  assert.equal(stillUnknown.status, "cancelled");
  assert.equal(protocol.needsResultVerification(stillUnknown), true);
  const verified = await protocol.verifyCancelledRun(stillUnknown);
  assert.equal(verified.status, "cancelled");
  assert.equal(protocol.needsResultVerification(verified), false);
  assert.equal(verified.tools[0].finished.ok, true);
  assert.match(protocol.runNotice(verified), /未撤销/);
  assert.deepEqual(requests, [["/api/runs/r1", "GET"], ["/api/runs/r1", "GET"]]);
});

test("cancelled executing results can be verified while known terminal results do not offer verification", () => {
  const cancelled = snapshot({ status: "cancelled" });
  cancelled.invocations[0].status = "executing";
  assert.equal(protocol.needsResultVerification(protocol.restoreRun(cancelled)), true);
  cancelled.invocations[0].status = "succeeded";
  assert.equal(protocol.needsResultVerification(protocol.restoreRun(cancelled)), false);
});
