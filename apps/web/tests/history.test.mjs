import assert from 'node:assert/strict';
import {test, afterEach} from 'node:test';
import {execFileSync} from 'node:child_process';
execFileSync('node', ['node_modules/typescript/bin/tsc','src/history.ts','--outDir','.test-build','--target','ES2022','--module','ESNext','--skipLibCheck']);
const {parseSessionPage, loadSessionPage, SessionRequests, historyStatusLabel} = await import('../.test-build/history.js');
const originalFetch=globalThis.fetch;
afterEach(()=>{globalThis.fetch=originalFetch;});
const item={session_id:'old',title:'报价任务',updated_at:'2026-10-11T02:00:00Z',status:'waiting_approval'};
test('history validates empty pages, titles, dates and every RunStore status',()=>{
  assert.deepEqual(parseSessionPage({items:[],has_more:false}),{items:[],has_more:false});
  for(const status of ['running','waiting_approval','recovering','reconciliation_required','unknown','completed','failed','cancelled']) {
    assert.equal(parseSessionPage({items:[{...item,status}],has_more:true}).items[0].status,status);
    assert.ok(historyStatusLabel(status));
  }
  for(const invalid of [{...item,status:'invented'},{...item,updated_at:'bad'},{...item,session_id:''},{...item,title:1}]) assert.throws(()=>parseSessionPage({items:[invalid],has_more:false}),/历史/);
  assert.throws(()=>parseSessionPage({items:[],has_more:'false'}),/历史/);
});
test('history paging sends only limit and offset and preserves structured unavailable errors',async()=>{
  const signal=new AbortController().signal;
  globalThis.fetch=async(url,options)=>{assert.equal(url,'/api/sessions?limit=20&offset=20');assert.equal(options.signal,signal);return Response.json({items:[item],has_more:false});};
  assert.deepEqual(await loadSessionPage(20,signal),{items:[item],has_more:false});
  globalThis.fetch=async()=>Response.json({error:{code:'history_disabled',message:'历史不可用'}},{status:503});
  await assert.rejects(loadSessionPage(0),error=>error.status===503 && error.code==='history_disabled');
});
test('session requests invalidate old success, failure and stream frames even when returning to the same session',()=>{
  const requests=new SessionRequests('a');
  const old=requests.capture();
  const refresh=requests.refresh();
  const latest=requests.refresh();
  assert.equal(requests.isCurrent(refresh),false);
  assert.equal(requests.isCurrent(latest),true);
  assert.equal(requests.isCurrent(old),true);
  requests.switchTo('b');
  assert.equal(requests.isCurrent(old),false);
  assert.equal(requests.isCurrent(latest),false);
  requests.switchTo('a');
  assert.equal(requests.isCurrent(old),false);
  assert.equal(requests.isCurrent(requests.capture()),true);
});
