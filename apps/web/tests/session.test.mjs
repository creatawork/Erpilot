import assert from "node:assert/strict";
import {test} from "node:test";
import {hydrateTurns} from "../.test-build/session.js";

test("hydration pairs tool results and restores normalized pending arguments", () => {
  const turns = hydrateTurns({session_id: "s", status: "waiting_approval", messages: [
    {role: "system", content: "system"},
    {role: "user", content: "write"},
    {role: "assistant", content: "checking", tool_calls: [
      {id: "r", function: {name: "read", arguments: "{}"}},
      {id: "w", function: {name: "write", arguments: '{"sku":"A"}'}},
    ]},
    {role: "tool", tool_call_id: "r", content: "result"},
  ], tool_results: [{call_id: "r", name: "read", content: "failed result", ok: false}],
  pending_approvals: [{call_id: "w", pending_id: "p", tool: "write", risk: "confirm",
    arguments: {sku: "A", client_token: "stable"}}]});
  assert.equal(turns.length, 2);
  assert.equal(turns[1].tools[0].finished.content, "failed result");
  assert.equal(turns[1].tools[0].finished.ok, false);
  assert.equal(turns[1].tools[1].approval.pending_id, "p");
  assert.deepEqual(JSON.parse(turns[1].tools[1].arguments), {sku: "A", client_token: "stable"});
});
