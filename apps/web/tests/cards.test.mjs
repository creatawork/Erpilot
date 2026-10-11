import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

buildSync({entryPoints: ["src/components/AssistantMessage.tsx"], bundle: true,
  platform: "node", format: "esm", outfile: ".test-build/assistant-message.mjs",
  packages: "external", jsx: "automatic"});
const { AssistantMessage } = await import("../.test-build/assistant-message.mjs");
buildSync({entryPoints:['src/components/OrderDraftPanel.tsx'],bundle:true,platform:'node',format:'esm',outfile:'.test-build/order-draft-panel.mjs',packages:'external',jsx:'automatic'});
const { OrderDraftPanel, DraftSummary } = await import('../.test-build/order-draft-panel.mjs');

test('draft summary shows exact approved customer, prices and stock impact',()=>{
  const draft={draft_id:'d',revision:1,customer:'原客户',note:'原备注',items:[{sku:'A',name:'商品',quantity:9,base_price:100,discount:1,unit_price:100,line_total:900,stock_before:20,stock_after:11}],total_amount:900,expires_at:'2099-01-01T00:00:00Z',status:'open',order_id:null};
  const html=renderToStaticMarkup(React.createElement(DraftSummary,{draft}));
  for(const text of ['原客户','原备注','¥900.00','¥100.00','20 → 11','版本 1']) assert.ok(html.replace(/<!--.*?-->/g,'').includes(text),text);
});
test('pending approval locks new submission and editor controls have accessible labels',()=>{
  const html=renderToStaticMarkup(React.createElement(OrderDraftPanel,{drafts:[],loading:false,error:'',busy:false,canSubmit:false,hasPending:true,onRefresh(){},async onSave(){},onSubmit(){}}));
  for(const text of ['草稿客户','第 1 行 SKU','第 1 行数量','删除第 1 行','先拒绝旧审批']) assert.ok(html.includes(text),text);
});
test('a submitted draft still offers an accessible new draft action',()=>{
  const draft={draft_id:'done',revision:1,customer:'已提交客户',note:null,items:[{sku:'A',name:'商品',quantity:1,base_price:100,discount:1,unit_price:100,line_total:100,stock_before:20,stock_after:19}],total_amount:100,expires_at:'2099-01-01T00:00:00Z',status:'submitted',order_id:'order-1'};
  const html=renderToStaticMarkup(React.createElement(OrderDraftPanel,{drafts:[draft],loading:false,error:'',busy:false,canSubmit:true,hasPending:false,onRefresh(){},async onSave(){},onSubmit(){}}));
  assert.match(html,/<button type="button">新建草稿<\/button>/);
});
test('read-only runtime keeps saving available and explains disabled order submission',()=>{
  const draft={draft_id:'open',revision:1,customer:'客户',note:null,items:[{sku:'A',name:'商品',quantity:1,base_price:100,discount:1,unit_price:100,line_total:100,stock_before:20,stock_after:19}],total_amount:100,expires_at:'2099-01-01T00:00:00Z',status:'open',order_id:null};
  const html=renderToStaticMarkup(React.createElement(OrderDraftPanel,{drafts:[draft],loading:false,error:'',busy:false,canSubmit:true,writesEnabled:false,hasPending:false,onRefresh(){},async onSave(){},onSubmit(){}}));
  assert.match(html,/草稿可保存，下单未开放/);
  assert.match(html,/<button type="button" disabled="">请求提交版本/);
  assert.doesNotMatch(html,/<input aria-label="草稿客户" disabled/);
});

test("live reasoning is expanded while completed reasoning is collapsed", () => {
  for (const live of [true, false]) {
    const html = renderToStaticMarkup(React.createElement(AssistantMessage, {
      turn: {role: "assistant", phase: live ? "analyzing" : "completed", step: 1,
        blocks: [{id: "r", type: "reasoning", step: 1, text: "正在核对实际库存"}],
        tools: {}, phaseAt: 0, done: null, error: null},
      now: 1000, live, stalled: false, submittingApprovals: new Set(), onRespond() {},
    }));
    assert.equal(/<details class="reasoning" open="">/.test(html), live);
    if (live) assert.match(html, /正在思考/);
  }
});

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
