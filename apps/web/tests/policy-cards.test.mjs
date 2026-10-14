import assert from 'node:assert/strict';
import {test} from 'node:test';
import {buildSync} from 'esbuild';
import {createRequire} from 'node:module';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
createRequire(import.meta.url).extensions['.css'] = () => {};

export const assessment = {
  schema_version: 1, assessed_at: '2026-10-12T12:00:00Z',
  order: {order_id: 'SO-T', customer: '甲', status: '待付款', total_amount: 19.5},
  business_allowed: true,
  business_reason: {code: 'cancellable_status', message: '当前状态允许发起取消申请'},
  policy_state: 'matched', decision: 'eligible',
  message: '当前可以发起取消申请，仍需审批；本次仅查询，未执行取消。',
  references: [{source: 'order-policy.md', title: '可取消的订单', text: '待付款待发货可取消',
    score: 0.9, policy_id: 'order', version: '2026-10-12.1',
    effective_from: '2026-10-11T16:00:00Z', effective_until: null}],
};
buildSync({entryPoints:['src/components/BusinessResultCard.tsx'],bundle:true,platform:'node',
  format:'esm',outfile:'.test-build/policy-result.mjs',packages:'external',jsx:'automatic'});
const {BusinessResultCard,isUsableDisplay}=await import('../.test-build/policy-result.mjs');
buildSync({entryPoints:['src/components/AssistantMessage.tsx'],bundle:true,platform:'node',
  format:'esm',outfile:'.test-build/policy-assistant.mjs',packages:'external',jsx:'automatic',
  alias:{'@ant-design/x':'./tests/ant-design-x-node.mjs'}});
const {AssistantMessage}=await import('../.test-build/policy-assistant.mjs');
const display=proof=>({version:1,kind:'cancellation_assessment',outcome:'succeeded',assessment:proof});
const html=proof=>renderToStaticMarkup(React.createElement(BusinessResultCard,{display:display(proof)})).replace(/<!--.*?-->/g,'');

test('query card shows authoritative facts, policy version and no cancel controls',()=>{
  const result=html(assessment);
  for(const text of ['可发起取消申请','查询完成','仍需审批','未执行取消','SO-T','待付款','¥19.50','2026-10-12.1','order-policy.md'])assert.ok(result.includes(text),text);
  assert.match(result,/<details[^>]*><summary>查看政策依据<\/summary>/);
  assert.doesNotMatch(result,/<button|审批通过|取消成功/);
});
test('policy gaps and domain prohibition are distinct query outcomes',()=>{
  for(const [decision,policy_state,business_allowed,status,label] of [
    ['needs_review','unavailable',true,'待付款','政策待核对'],
    ['ineligible','unavailable',false,'已发货','当前不可取消'],
  ]){
    const result=html({...assessment,decision,policy_state,business_allowed,
      order:{...assessment.order,status},references:[],business_reason:{
        code:business_allowed?'cancellable_status':'invalid_transition',message:'当前状态判断'},message:'本次仅查询，未执行取消。'});
    assert.ok(result.includes(label));assert.ok(result.includes('查询完成'));
  }
});
test('reference text is escaped and never becomes markup or an executable link',()=>{
  const result=html({...assessment,references:[{...assessment.references[0],
    source:'<img src=x onerror=alert(1)>',text:'<script>alert(1)</script> javascript:alert(1)'}]});
  assert.ok(result.includes('&lt;script&gt;'));
  assert.doesNotMatch(result,/<script|<img|href="javascript:/);
});
test('bad versions, numbers and inconsistent decisions fall back to raw detail',()=>{
  for(const proof of [{...assessment,schema_version:true},{...assessment,business_allowed:1},
    {...assessment,order:{...assessment.order,total_amount:NaN}},
    {...assessment,policy_state:'unknown'}, {...assessment,references:[]},
    {...assessment,references:[{...assessment.references[0],score:Infinity}]},
    {...assessment,assessed_at:'2026-10-12'},
  ])assert.equal(isUsableDisplay(display(proof)),false);
  assert.equal(isUsableDisplay({...display(assessment),version:2}),false);
  assert.equal(isUsableDisplay({...display(assessment),kind:'future'}),false);
  const invalid=display({...assessment,schema_version:2});
  const entity={id:'c',name:'assess_order_cancellation',arguments:'{"order_id":"SO-T"}',status:'succeeded',display:invalid,finished:{content:'raw assessment evidence'},approval:null,approvalResolved:null};
  const result=renderToStaticMarkup(React.createElement(AssistantMessage,{turn:{role:'assistant',phase:'completed',blocks:[{id:'b',type:'tool',callId:'c'}],tools:{c:entity},step:1,phaseAt:0,done:null,error:null},now:0,live:false,stalled:false,submittingApprovals:new Set(),onRespond(){}}));
  assert.ok(result.includes('raw assessment evidence'));
  assert.ok(!result.includes('可发起取消申请'));
});
