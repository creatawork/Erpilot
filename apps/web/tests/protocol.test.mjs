import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { submitApproval } from "../.test-build/protocol.js";

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
