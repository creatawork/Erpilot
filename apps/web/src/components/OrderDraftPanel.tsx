import { useEffect, useState } from 'react';
import { type DraftInput, type OrderDraft } from '../drafts';

const money = (value: number) => `¥${value.toFixed(2)}`;
const statuses = {open:'待提交',expired:'已过期',superseded:'旧版本',submitted:'已提交'};
export function DraftSummary({draft}: {draft: OrderDraft}) {
  return <div className="draft-summary">
    <p>{draft.customer} · 版本 {draft.revision} · {statuses[draft.status]}</p>
    <div className="draft-table"><table><caption>报价与库存影响</caption><thead><tr><th>商品 / SKU</th><th>数量</th><th>原价</th><th>折扣</th><th>单价</th><th>小计</th><th>库存前 → 后</th></tr></thead><tbody>
      {draft.items.map(line => <tr key={line.sku}><td>{line.name}<br />{line.sku}</td><td>{line.quantity}</td><td>{money(line.base_price)}</td><td>{Math.round(line.discount * 100)}%</td><td>{money(line.unit_price)}</td><td>{money(line.line_total)}</td><td>{line.stock_before} → {line.stock_after}</td></tr>)}
    </tbody></table></div>
    <p><strong>总金额 {money(draft.total_amount)}</strong> · 报价有效至 <time dateTime={draft.expires_at}>{new Date(draft.expires_at).toLocaleString()}</time></p>
    {draft.note && <p>备注：{draft.note}</p>}{draft.order_id && <p>订单：{draft.order_id}</p>}
  </div>;
}
export function OrderDraftPanel({drafts,loading,error,busy,canSubmit,writesEnabled=true,hasPending,onRefresh,onSave,onSubmit}: {
  drafts:OrderDraft[]; loading:boolean; error:string; busy:boolean; canSubmit:boolean; writesEnabled?:boolean; hasPending:boolean;
  onRefresh:()=>void; onSave:(draft:OrderDraft | undefined,input:DraftInput)=>Promise<OrderDraft | void>; onSubmit:(draft:OrderDraft)=>void;
}) {
  const [selected,setSelected] = useState<string | null | undefined>(undefined);
  const draft = selected===null ? undefined : drafts.find(item=>item.draft_id===selected) ?? drafts[0];
  const [customer,setCustomer] = useState('');
  const [note,setNote] = useState('');
  const [lines,setLines] = useState([{sku:'',quantity:'1'}]);
  const [saving,setSaving] = useState(false);
  const [saveError,setSaveError] = useState('');
  useEffect(()=>{setCustomer(draft?.customer??'');setNote(draft?.note??'');setLines(draft?.items.map(item=>({sku:item.sku,quantity:String(item.quantity)}))??[{sku:'',quantity:'1'}]);setSaveError('');},[draft?.draft_id,draft?.revision]);
  const original = draft ? JSON.stringify({customer:draft.customer,note:draft.note??'',items:draft.items.map(item=>({sku:item.sku,quantity:String(item.quantity)}))}) : '';
  const dirty = original !== JSON.stringify({customer,note,items:lines});
  const valid = !!customer.trim() && lines.length>0 && lines.every(line=>!!line.sku.trim() && Number.isSafeInteger(Number(line.quantity)) && Number(line.quantity)>0);
  const locked = busy || saving || draft?.status==='submitted';
  return <section className="draft-panel" aria-label="订单草稿" aria-busy={loading || saving}>
    <div className="draft-heading"><h2>订单草稿</h2><button type="button" onClick={()=>{setSelected(null);setCustomer('');setNote('');setLines([{sku:'',quantity:'1'}]);setSaveError('');}} disabled={busy||saving}>新建草稿</button><button type="button" onClick={onRefresh} disabled={loading||saving}>刷新</button></div>
    {drafts.length>0 && <label>选择草稿 <select value={draft?.draft_id??''} onChange={event=>setSelected(event.target.value||null)} disabled={saving}><option value="">新草稿</option>{drafts.map(item=><option key={item.draft_id} value={item.draft_id}>{item.customer} · 版本 {item.revision}</option>)}</select></label>}
    {loading && <p role="status">正在核对草稿…</p>}{error && <p role="alert">{error}</p>}
    <form onSubmit={async event=>{event.preventDefault();if(!valid||locked)return;setSaving(true);setSaveError('');try{const saved=await onSave(draft,{customer:customer.trim(),note,items:lines.map(line=>({sku:line.sku.trim(),quantity:Number(line.quantity)}))});if(saved)setSelected(saved.draft_id);}catch(error){setSaveError(error instanceof Error?error.message:String(error));}finally{setSaving(false);}}}>
      <label>客户<input aria-label="草稿客户" value={customer} onChange={event=>setCustomer(event.target.value)} disabled={locked} required /></label>
      {lines.map((line,index)=><div className="draft-line" key={index}>
        <label>SKU<input aria-label={`第 ${index+1} 行 SKU`} value={line.sku} onChange={event=>setLines(prev=>prev.map((item,i)=>i===index?{...item,sku:event.target.value}:item))} disabled={locked} required /></label>
        <label>数量<input aria-label={`第 ${index+1} 行数量`} type="number" min="1" step="1" value={line.quantity} onChange={event=>setLines(prev=>prev.map((item,i)=>i===index?{...item,quantity:event.target.value}:item))} disabled={locked} required /></label>
        <button type="button" aria-label={`删除第 ${index+1} 行`} onClick={()=>setLines(prev=>prev.filter((_,i)=>i!==index))} disabled={locked||lines.length===1}>删除</button>
      </div>)}
      <label>备注<input aria-label="草稿备注" value={note} onChange={event=>setNote(event.target.value)} disabled={locked}/></label>
      <div className="draft-actions"><button type="button" onClick={()=>setLines(prev=>[...prev,{sku:'',quantity:'1'}])} disabled={locked}>添加商品</button><button type="submit" disabled={locked||!valid}>{saving?'保存中…':draft?'保存 / 刷新报价':'创建草稿'}</button></div>
    </form>
    {saveError && <p role="alert">{saveError}</p>}
    {draft && <><DraftSummary draft={draft}/><button type="button" disabled={!writesEnabled||!canSubmit||saving||dirty||draft.status!=='open'||Date.parse(draft.expires_at)<=Date.now()} onClick={()=>onSubmit(draft)}>请求提交版本 {draft.revision} 并审批</button></>}
    {!writesEnabled && <p role="status">草稿可保存，下单未开放。</p>}
    {dirty && draft && <p role="status">修改后请先保存，以新的报价版本申请审批。</p>}
    {hasPending && <p role="status">已有待处理审批；修改草稿后，请先拒绝旧审批，再提交新版本。</p>}
  </section>;
}
