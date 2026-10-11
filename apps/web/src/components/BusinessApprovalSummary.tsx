import type { ApprovalPendingPayload } from '../protocol';
import { approvalOperationIssue, type BusinessOperation } from '../operations';

export function BusinessApprovalSummary({pending,operation,verified,enabled,loading,error,now,submitting,onRespond,onRefresh}:{pending:ApprovalPendingPayload;operation?:BusinessOperation;verified:boolean;enabled:boolean;loading:boolean;error?:string;now:number;submitting:boolean;onRespond:(id:string,approved:boolean)=>void;onRefresh:()=>void}) {
  const issue=!enabled?'当前尚未开放此业务审批，请拒绝后重新准备。':error||approvalOperationIssue(pending,operation,verified,now);
  return <section className="approval business-approval" aria-label={pending.tool==='cancel_order'?'取消订单审批':'库存调整审批'}>
    <span className="approval-label">待审批 · {pending.tool==='cancel_order'?'取消订单':'库存调整'}</span>
    {operation?.kind==='cancel_order'&&<>
      <p>订单 <strong>{operation.summary.order_id}</strong> · 客户 {operation.summary.customer}</p>
      <p>状态 {operation.summary.status_before} → {operation.summary.status_after} · 原单金额 ¥{operation.summary.total_amount.toFixed(2)}</p>
      {operation.summary.note&&<p>备注：{operation.summary.note}</p>}
      <p>取消订单不执行退款。</p>
      <div className="business-table"><table><caption>取消后的库存回补</caption><thead><tr><th>商品</th><th>数量</th><th>原单价格</th><th>库存变化 / 仓库</th></tr></thead><tbody>{operation.summary.items.map((line,index)=><tr key={`${line.sku}-${index}`}><td>{line.name} · {line.sku}</td><td>{line.quantity}</td><td>¥{line.unit_price.toFixed(2)} · 合计 ¥{line.line_total.toFixed(2)}</td><td>{line.stock_before===null?'无库存记录，无法回补':`${line.stock_before} → ${line.stock_after} · ${line.warehouse}`}</td></tr>)}</tbody></table></div>
    </>}
    {operation?.kind==='adjust_stock'&&<>
      <p>商品 <strong>{operation.summary.name}</strong> · {operation.summary.sku} · {operation.summary.warehouse}</p>
      <p>库存 {operation.summary.quantity_before} → <strong>{operation.summary.quantity_after}</strong> 件 · {operation.summary.delta>=0?'增加':'减少'} {Math.abs(operation.summary.delta)} 件</p>
      {!operation.summary.stock_exists&&<p>当前无库存记录，将创建主仓库存记录。</p>}
    </>}
    {issue&&<p role="status">{issue}</p>}
    <button className="approve" disabled={!!issue||loading||submitting} onClick={()=>onRespond(pending.pending_id,true)}>批准</button>
    <button className="deny" disabled={submitting} onClick={()=>onRespond(pending.pending_id,false)}>拒绝</button>
    <button disabled={loading||submitting} onClick={onRefresh}>{loading?'正在核对…':'刷新审批明细'}</button>
    <details className="business-technical"><summary>技术详情</summary><pre className="args">{JSON.stringify(pending.arguments)}</pre></details>
  </section>;
}
