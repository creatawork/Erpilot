import assert from 'node:assert/strict';
import {test, afterEach} from 'node:test';
import {parseOperation, getOperation, approvalOperationIssue, retainOperationSnapshot, requiresBusinessSummary} from '../.test-build/operations.js';
export const operation={operation_id:'op1',kind:'adjust_stock',status:'open',expires_at:'2099-01-01T00:00:00Z',invalid_reason:null,summary:{sku:'A',name:'商品',delta:-3,quantity_before:20,quantity_after:17,warehouse:'主仓',stock_exists:true}};
const pending={tool:'adjust_stock',arguments:{operation_id:'op1'}};
const originalFetch=globalThis.fetch;
test('legacy custom approvals keep generic decisions while scoped or current-provider approvals fail closed',()=>{
  for(const tool of ['cancel_order','adjust_stock']){
    assert.equal(requiresBusinessSummary({tool,arguments:{order_id:'123',sku:'A',delta:3}},false),false);
    assert.equal(requiresBusinessSummary({tool,arguments:{operation_id:'op1'}},false),true);
    assert.equal(requiresBusinessSummary({tool,arguments:{order_id:'123',sku:'A',delta:3}},true),true);
  }
  assert.equal(requiresBusinessSummary({tool:'create_order',arguments:{}},true),false);
});
afterEach(()=>{globalThis.fetch=originalFetch;});
test('operation client validates exact session reference and malformed snapshots',async()=>{
  globalThis.fetch=async url=>{assert.equal(url,'/api/sessions/s%2Fa/operations/op%2F1');return Response.json({...operation,operation_id:'op/1'});};
  assert.equal((await getOperation('s/a','op/1')).summary.delta,-3);
  for(const bad of [{...operation,summary:{...operation.summary,delta:NaN}},{...operation,status:'bogus'},{...operation,summary:{...operation.summary,stock_exists:'yes'}},{...operation,summary:{...operation.summary,quantity_after:-1}}])assert.throws(()=>parseOperation(bad),/格式/);
});
test('approval fails closed without verification or matching operation type and reference',()=>{
  assert.equal(approvalOperationIssue(pending,operation,true),null);
  assert.match(approvalOperationIssue(pending,operation,false),/核对/);
  assert.match(approvalOperationIssue({...pending,tool:'cancel_order'},operation,true),/不匹配/);
  assert.match(approvalOperationIssue({...pending,arguments:{operation_id:'other'}},operation,true),/不匹配/);
  for(const status of ['changed','expired','executed'])assert.match(approvalOperationIssue(pending,{...operation,status},true),/重新/);
  assert.match(approvalOperationIssue(pending,operation,true,Date.parse(operation.expires_at)),/过期/);
});
test('refresh preserves immutable approved business impact and refuses replacement snapshots',()=>{
  const current={...operation,status:'changed',invalid_reason:'库存已变化'};
  assert.deepEqual(retainOperationSnapshot(operation,current),current);
  const replaced={...operation,summary:{...operation.summary,quantity_after:16}};
  assert.throws(()=>retainOperationSnapshot(operation,replaced),/明细/);
});
test('operation API errors expose actionable domain hints',async()=>{
  globalThis.fetch=async()=>Response.json({error:{code:'operation_missing',message:'找不到预览',hint:'重新准备'}},{status:404});
  await assert.rejects(getOperation('s','op1'),/重新准备/);
});
test('cancel snapshots validate nullable stock impact as one complete missing-record state',()=>{
  const line={sku:'A',name:'商品',quantity:1,unit_price:100,line_total:100,stock_before:null,stock_after:null,warehouse:null};
  const cancellation={...operation,kind:'cancel_order',summary:{order_id:'123',customer:'客户',status_before:'pending_payment',status_after:'cancelled',total_amount:100,note:null,items:[line]}};
  assert.deepEqual(parseOperation(cancellation),cancellation);
  for(const items of [[],[{...line,stock_before:0}],[{...line,warehouse:'主仓'}],[{...line,unit_price:Infinity}]])assert.throws(()=>parseOperation({...cancellation,summary:{...cancellation.summary,items}}),/格式/);
});
