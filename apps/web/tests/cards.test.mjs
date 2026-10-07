import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

buildSync({entryPoints: ["src/components/AssistantMessage.tsx"], bundle: true,
  platform: "node", format: "esm", outfile: ".test-build/assistant-message.mjs",
  packages: "external", jsx: "automatic"});
const { AssistantMessage } = await import("../.test-build/assistant-message.mjs");

test("invalid business cards retain raw result details", () => {
  for (const display of [
    {version: 1, kind: "future", outcome: "succeeded"},
    {version: 1, kind: "stock_adjustment", outcome: "failed"},
    {version: 1, kind: "stock_query", outcome: "unknown"},
    {version: 1, kind: "order_creation", outcome: "denied"},
    {version: 1, kind: "stock_query", outcome: "succeeded", quantity: NaN},
    {version: 1, kind: "order_creation", outcome: "succeeded", order_id: {}},
  ]) {
    const entity = {id: "c", name: "adjust_stock", arguments: "null", status: "failed",
      display, finished: {content: "原始业务结果"}, approval: null, approvalResolved: null};
    const html = renderToStaticMarkup(React.createElement(AssistantMessage, {
      turn: {role: "assistant", phase: "completed", blocks: [{id: "b", type: "tool", callId: "c"}],
        tools: {c: entity}, step: 1, phaseAt: 0, done: null, error: null},
      now: 0, live: false, stalled: false, submittingApprovals: new Set(), onRespond() {},
    }));
    assert.match(html, /原始业务结果/);
  }
});
