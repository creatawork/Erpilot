// Scripted UI fixture: no language model, database or ERP connection.
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
let status='open';let pending=true;let approved=false;let mode='cancel_order';
const cancel={order_id:'123',customer:'浏览器验收客户',status_before:'待付款',status_after:'已取消',total_amount:300,note:'原审批备注',items:[{sku:'A',name:'验收商品',quantity:3,unit_price:100,line_total:300,stock_before:20,stock_after:23,warehouse:'主仓'},{sku:'B',name:'无库存商品',quantity:1,unit_price:0,line_total:0,stock_before:null,stock_after:null,warehouse:null}]};
const adjust={sku:'A',name:'验收商品',delta:-3,quantity_before:20,quantity_after:17,warehouse:'主仓',stock_exists:true};
const operation=()=>({operation_id:'fixture-operation',kind:mode,status,expires_at:'2099-01-01T00:00:00Z',invalid_reason:status==='changed'?'库存已变化':null,summary:mode==='cancel_order'?cancel:adjust});
const approval=()=>({call_id:'fixture-call',pending_id:'fixture-pending',tool:mode,risk:'single_confirm',arguments:{operation_id:'fixture-operation'},operation:operation()});
const result=()=>({call_id:'fixture-call',name:mode,content:JSON.stringify({ok:true}),ok:true,display:{version:1,kind:mode==='cancel_order'?'order_cancellation':'stock_adjustment',outcome:approved?'succeeded':'denied',...(mode==='cancel_order'?{order_id:'123',customer:cancel.customer,status:'已取消',total_amount:300}:{sku:'A',delta:-3,quantity_after:17}),message:approved?'操作已完成':'用户拒绝，未执行'}});
const snapshot=session_id=>({session_id,status:pending?'waiting_approval':'completed',messages:[{role:'user',content:mode==='cancel_order'?'取消订单 123':'调整库存'},{role:'assistant',tool_calls:[{id:'fixture-call',function:{name:mode,arguments:JSON.stringify({operation_id:'fixture-operation'})}}]}],pending_approvals:pending?[approval()]:[],tool_results:pending?[]:[result()]});
const handler=async(req,res,next)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/fixture/changed'){status='changed';res.end('changed');return;}
  if(url.pathname==='/fixture/reset'){status='open';pending=true;mode=url.searchParams.get('kind')==='adjust_stock'?'adjust_stock':'cancel_order';res.end('reset');return;}
  if(!req.url.startsWith('/api/'))return next();
  let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):{};
  const json=value=>{res.setHeader('content-type','application/json');res.end(JSON.stringify(value));};
  if(url.pathname==='/api/runtime')return json({data_source:'demo',writes_enabled:true,drafts_enabled:false,business_approvals_enabled:true});
  if(url.pathname.endsWith('/state'))return json(snapshot(url.pathname.split('/')[3]));
  if(url.pathname.includes('/operations/'))return json(operation());
  if(url.pathname==='/api/chat/approve/stream'){
    approved=body.approved;pending=false;status=approved?'executed':status;
    const r=result();res.setHeader('content-type','text/event-stream');res.end([['approval_resolved',{...approval(),approved,reason:'用户选择'}],['tool_finished',{...r,id:r.call_id}],['done',{steps:1,completed:true,usage:null,cost:null,duration_ms:1,trace:null}]].map(([event,data])=>`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''));return;
  }
  if(url.pathname.endsWith('/cancel')){pending=false;return json(snapshot(url.pathname.split('/')[3]));}
  res.statusCode=404;json({error:{message:'fixture route missing'}});
};
const server=await createServer({configFile:false,plugins:[react(),{name:'scripted-business-api',configureServer(server){server.middlewares.use(handler);}}],server:{host:'127.0.0.1',port:5179,strictPort:true}});
await server.listen();console.log('Scripted business approval UI fixture: http://127.0.0.1:5179');
