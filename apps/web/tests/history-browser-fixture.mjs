// Deterministic history acceptance; never connects to an LLM or ERP.
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
const pending={call_id:'history-call',pending_id:'history-pending',tool:'fixture_write',risk:'single_confirm',arguments:{note:'保留原审批'}};
const summary=(session_id,title,status='completed')=>({session_id,title,status,updated_at:'2026-10-11T02:00:00Z'});
const items=[summary('approval','审批会话','waiting_approval'),summary('done','已完成报价'),summary('interrupted','中断任务','recovering'),summary('broken','读取失败'),summary('slow','延迟恢复失败'),summary('live','活动任务','running'),...Array.from({length:15},(_,i)=>summary(`page-${i}`,`历史报价 ${i+1}`))];
const snapshot=session_id=>({session_id,status:session_id==='approval'?'waiting_approval':session_id==='interrupted'?'interrupted':session_id==='live'?'running':'completed',messages:[{role:'user',content:session_id==='approval'?'请确认原操作':session_id==='done'?'已完成报价内容':session_id==='interrupted'?'中断任务内容':`会话 ${session_id}`}],pending_approvals:session_id==='approval'?[pending]:[],tool_results:[],...(session_id==='approval'?{run_id:'old-run',last_seq:2}:{})});
let cancelled=0, mutations=0;
const requests=[];
const handler=async(req,res,next)=>{
 const url=new URL(req.url,'http://localhost');
 if(!url.pathname.startsWith('/api/'))return next();
 requests.push({path:url.pathname,method:req.method,query:url.search});
 const json=(value,status=200)=>{res.statusCode=status;res.setHeader('content-type','application/json');res.end(JSON.stringify(value));};
 if(url.pathname==='/api/runtime')return json({data_source:'demo',writes_enabled:true,drafts_enabled:false,business_approvals_enabled:false});
 if(url.pathname==='/api/sessions'){const offset=Number(url.searchParams.get('offset'));return json({items:items.slice(offset,offset+20),has_more:offset+20<items.length});}
 if(url.pathname==='/api/fixture-evidence')return json({cancelled,mutations,requests});
 if(url.pathname==='/api/runs/old-run/events'){
   res.setHeader('content-type','text/event-stream');
   const timer=setTimeout(()=>res.end('event: delta\ndata: {"text":"旧流绝不能串会话"}\n\nevent: error\ndata: {"message":"旧流失败绝不能串会话"}\n\n'),3000);
   res.on('close',()=>clearTimeout(timer));return;
 }
 if(url.pathname.endsWith('/state')){
   const id=url.pathname.split('/')[3];
   if(id==='broken')return json({error:{message:'脚本恢复失败'}},503);
   if(id==='slow'){setTimeout(()=>json({error:{message:'旧请求延迟失败'}},503),3000);return;}
   return json(snapshot(id));
 }
 if(req.method!=='GET'){mutations++;if(url.pathname.endsWith('/cancel'))cancelled++;return json({error:{message:'验收不应执行写操作'}},400);}
 return json({error:{message:'Fixture route missing'}},404);
};
const server=await createServer({configFile:false,plugins:[react(),{name:'history-fixture',configureServer(server){server.middlewares.use(handler);}}],server:{host:'127.0.0.1',port:5181,strictPort:true}});
await server.listen();console.log('History fixture: http://127.0.0.1:5181');
