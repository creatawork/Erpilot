// Local scripted UI fixture. Never connects to an ERP or language model.
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
let revision=1;
const submitted=process.argv.includes('--submitted');
const readOnly=process.argv.includes('--readonly');
let pending=!submitted;
let additionalDraft;
let quantities=[9];
let customer='浏览器验收客户';
const versions=new Map();
function quote(){const items=quantities.map((quantity,index)=>({sku:index?'B':'A',name:index?'第二商品':'验收商品',quantity,base_price:100,discount:quantity>=10?.98:1,unit_price:quantity>=10?98:100,line_total:quantity*(quantity>=10?98:100),stock_before:100,stock_after:100-quantity}));return {draft_id:'fixture-draft',revision,customer,note:'脚本数据，不写入真实订单',items,total_amount:items.reduce((sum,line)=>sum+line.line_total,0),expires_at:'2099-01-01T00:00:00Z',status:submitted?'submitted':'open',order_id:submitted?'fixture-order':null};}
versions.set(1,quote());
const approval=()=>({call_id:'fixture-call',pending_id:`fixture-p${revision}`,tool:'submit_order_draft',risk:'single_confirm',arguments:{draft_id:'fixture-draft',revision},draft:versions.get(revision)});
let oldApproval=approval();
const snapshot=session_id=>({session_id,status:pending?'waiting_approval':'completed',messages:[],pending_approvals:pending?[oldApproval]:[],tool_results:[]});
const handler=async(req,res,next)=>{
  if(!req.url.startsWith('/api/'))return next();
  let raw='';for await(const chunk of req)raw+=chunk;
  const body=raw?JSON.parse(raw):{};
  const url=new URL(req.url,'http://localhost');
  const json=value=>{res.setHeader('content-type','application/json');res.end(JSON.stringify(value));};
  const stream=events=>{res.setHeader('content-type','text/event-stream');res.end(events.map(([event,data])=>`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''));};
  if(url.pathname==='/api/runtime')return json({data_source:'demo',writes_enabled:!readOnly,drafts_enabled:true});
  if(url.pathname.endsWith('/state'))return json(snapshot(url.pathname.split('/')[3]));
  if(url.pathname.endsWith('/order-drafts')){
    if(req.method==='POST'){
      const items=body.items.map(line=>({sku:line.sku,name:'新草稿商品',quantity:line.quantity,base_price:100,discount:1,unit_price:100,line_total:line.quantity*100,stock_before:100,stock_after:100-line.quantity}));
      additionalDraft={...quote(),draft_id:'fixture-new',revision:1,customer:body.customer,note:body.note??null,items,total_amount:items.reduce((sum,line)=>sum+line.line_total,0),status:'open',order_id:null};
      res.statusCode=201;return json(additionalDraft);
    }
    return json({items:[...(additionalDraft?[additionalDraft]:[]),quote()]});
  }
  if(url.pathname.endsWith('/fixture-new')) return json(additionalDraft);
  if(url.pathname.endsWith('/fixture-draft')){
    if(req.method==='PATCH'){revision++;customer=body.customer;quantities=body.items.map(line=>line.quantity);versions.set(revision,quote());return json(quote());}
    return json(versions.get(Number(url.searchParams.get('revision'))||revision));
  }
  if(url.pathname==='/api/chat/approve/stream'){
    pending=false;return stream([['approval_resolved',{...oldApproval,approved:body.approved,reason:'浏览器验收'}],['done',{steps:1,completed:true,usage:null,cost:null,duration_ms:1,trace:null}]]);
  }
  if(url.pathname==='/api/chat/stream'){
    pending=true;oldApproval=approval();return stream([['start',{session_id:body.session_id,model:'scripted-fixture'}],['tool_started',{id:'fixture-call',name:'submit_order_draft',arguments:JSON.stringify(oldApproval.arguments)}],['approval_pending',oldApproval]]);
  }
  if(url.pathname.endsWith('/cancel')){pending=false;return json(snapshot(url.pathname.split('/')[3]));}
  res.statusCode=404;json({error:{message:'fixture route missing'}});
};
const server=await createServer({configFile:false,plugins:[react(),{name:'scripted-api',configureServer(server){server.middlewares.use(handler);}}],server:{host:'127.0.0.1',port:5178,strictPort:true}});
await server.listen();
console.log('Scripted draft UI fixture: http://127.0.0.1:5178');
