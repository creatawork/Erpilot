import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { parseOrderDraft, listOrderDrafts, getOrderDraft, createOrderDraft, reviseOrderDraft, approvalDraftIssue } from '../.test-build/drafts.js';
const draft = {draft_id:'d1', revision:1, customer:'客户', note:null, items:[{sku:'A',name:'商品',quantity:9,base_price:100,discount:1,unit_price:100,line_total:900,stock_before:20,stock_after:11}], total_amount:900,expires_at:'2099-01-01T00:00:00Z',status:'open',order_id:null};
const originalFetch = globalThis.fetch;
afterEach(() => {globalThis.fetch = originalFetch;});
test('draft parser rejects unsafe totals and malformed line items', () => {
  assert.deepEqual(parseOrderDraft(draft),draft);
  for (const invalid of [{...draft,total_amount:NaN},{...draft,revision:0},{...draft,items:[{...draft.items[0],quantity:1.5}]},{...draft,status:'bogus'}]) assert.throws(() => parseOrderDraft(invalid), /草稿/);
});
test('draft client preserves session ownership and exact revision URLs', async () => {
  const urls=[];
  globalThis.fetch=async url => {urls.push(url);return Response.json(url.endsWith('order-drafts')?{items:[draft]}:draft);};
  assert.deepEqual(await listOrderDrafts('s/a'),[draft]);
  await getOrderDraft('s/a','d1',1);
  assert.deepEqual(urls,['/api/sessions/s%2Fa/order-drafts','/api/sessions/s%2Fa/order-drafts/d1?revision=1']);
});
test('revision saves send optimistic version and propagate domain hints', async () => {
  globalThis.fetch=async (url,options) => {assert.equal(options.method,'PATCH');assert.equal(JSON.parse(options.body).expected_revision,1);return Response.json({error:{code:'draft_changed',message:'已修改',hint:'刷新后重试'}},{status:409});};
  await assert.rejects(reviseOrderDraft('s','d1',1,{customer:'客户',items:[{sku:'A',quantity:10}]}), error=>error.code==='draft_changed' && /刷新后重试/.test(error.message));
});
test('approval requires exact snapshot and a current live revision, while stale requests explain rejection', () => {
  const pending={arguments:{draft_id:'d1',revision:1}};
  assert.equal(approvalDraftIssue(pending,draft,[draft]),null);
  assert.match(approvalDraftIssue(pending,draft,[{...draft,revision:2}]),/拒绝/);
  assert.match(approvalDraftIssue(pending,undefined,[draft]),/明细/);
  assert.match(approvalDraftIssue(pending,{...draft,revision:2},[draft]),/版本/);
});
test('create draft sends only customer line requests and optional note',async()=>{
  const input={customer:'客户',items:[{sku:'A',quantity:9}],note:'备注'};
  globalThis.fetch=async(url,options)=>{assert.equal(url,'/api/sessions/s1/order-drafts');assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),input);return Response.json(draft,{status:201});};
  assert.deepEqual(await createOrderDraft('s1',input),draft);
});
test('expired or missing current drafts cannot be approved',()=>{
  const pending={arguments:{draft_id:'d1',revision:1}};
  assert.match(approvalDraftIssue(pending,draft,[]),/核对/);
  assert.match(approvalDraftIssue(pending,draft,[draft],Date.parse(draft.expires_at)),/过期/);
  assert.match(approvalDraftIssue(pending,draft,[{...draft,status:'submitted'}]),/提交/);
});
test('malformed lists fail closed and cancellation is passed through to fetch',async()=>{
  const controller=new AbortController();
  globalThis.fetch=async(url,options)=>{assert.equal(options.signal,controller.signal);return Response.json({items:[{...draft,items:[]} ]});};
  await assert.rejects(listOrderDrafts('s1',controller.signal),/草稿/);
});
