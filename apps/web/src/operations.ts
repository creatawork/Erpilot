export interface CancellationLine {sku:string; name:string; quantity:number; unit_price:number; line_total:number; stock_before:number|null; stock_after:number|null; warehouse:string|null}
export interface CancellationSummary {order_id:string;customer:string;status_before:string;status_after:string;total_amount:number;note:string|null;items:CancellationLine[]}
export interface AdjustmentSummary {sku:string;name:string;delta:number;quantity_before:number;quantity_after:number;warehouse:string;stock_exists:boolean}
interface OperationBase {operation_id:string;status:'open'|'changed'|'expired'|'executed';expires_at:string;invalid_reason:string|null}
export type BusinessOperation = OperationBase & ({kind:'cancel_order';summary:CancellationSummary}|{kind:'adjust_stock';summary:AdjustmentSummary});
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const text=(value:unknown)=>typeof value==='string'&&value.length>0;
const integer=(value:unknown)=>typeof value==='number'&&Number.isSafeInteger(value);
const quantity=(value:unknown)=>integer(value)&&Number(value)>=0;
const money=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
export function isBusinessApproval(tool:string){return tool==='cancel_order'||tool==='adjust_stock';}
export function requiresBusinessSummary(pending:{tool:string;arguments:Record<string,unknown>},enabled:boolean){
  return isBusinessApproval(pending.tool)&&(enabled||typeof pending.arguments.operation_id==='string');
}
export function parseOperation(value:unknown):BusinessOperation {
  const fail=()=>{throw new Error('操作预览格式无效，请拒绝后重新准备。');};
  if(!record(value)||!text(value.operation_id)||!['open','changed','expired','executed'].includes(String(value.status))||typeof value.expires_at!=='string'||!Number.isFinite(Date.parse(value.expires_at))||!(value.invalid_reason===null||typeof value.invalid_reason==='string')||!record(value.summary))return fail();
  const s=value.summary;
  if(value.kind==='adjust_stock') {
    if(!text(s.sku)||!text(s.name)||!integer(s.delta)||!quantity(s.quantity_before)||!quantity(s.quantity_after)||!text(s.warehouse)||typeof s.stock_exists!=='boolean')return fail();
  } else if(value.kind==='cancel_order') {
    if(!text(s.order_id)||!text(s.customer)||!text(s.status_before)||!text(s.status_after)||!money(s.total_amount)||!(s.note===null||typeof s.note==='string')||!Array.isArray(s.items)||!s.items.length)return fail();
    for(const line of s.items) {
      if(!record(line)||!text(line.sku)||!text(line.name)||!quantity(line.quantity)||Number(line.quantity)<1||!money(line.unit_price)||!money(line.line_total))return fail();
      const missing=line.stock_before===null&&line.stock_after===null&&line.warehouse===null;
      if(!missing&&(!quantity(line.stock_before)||!quantity(line.stock_after)||!text(line.warehouse)))return fail();
    }
  } else return fail();
  return value as unknown as BusinessOperation;
}
export async function getOperation(sessionId:string,operationId:string,signal?:AbortSignal):Promise<BusinessOperation>{
  const response=await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/operations/${encodeURIComponent(operationId)}`,{signal});
  const body=await response.json().catch(()=>null);
  if(!response.ok){const error=record(body)&&record(body.error)?body.error:{};throw new Error([error.message??`HTTP ${response.status}`,error.hint].filter(Boolean).join('；'));}
  return parseOperation(body);
}
export function retainOperationSnapshot(snapshot:BusinessOperation|undefined,current:BusinessOperation):BusinessOperation{
  // Current validity can change; the approved business impact cannot be replaced.
  if(snapshot&&(snapshot.operation_id!==current.operation_id||snapshot.kind!==current.kind||snapshot.expires_at!==current.expires_at||JSON.stringify(snapshot.summary)!==JSON.stringify(current.summary)))throw new Error('审批明细发生变化，请拒绝后重新准备。');
  return snapshot?{...snapshot,status:current.status,invalid_reason:current.invalid_reason}:current;
}
export function approvalOperationIssue(pending:{tool:string;arguments:Record<string,unknown>;expires_at?:string|null},operation:BusinessOperation|undefined,verified:boolean,now=Date.now()):string|null {
  if(!operation||!verified)return '审批明细尚未核对，请刷新；无法核对时请拒绝后重新准备。';
  if(operation.operation_id!==pending.arguments.operation_id||operation.kind!==pending.tool)return '审批对象不匹配，请拒绝后重新准备。';
  if(Date.parse(operation.expires_at)<=now||(pending.expires_at!=null&&(!Number.isFinite(Date.parse(pending.expires_at))||Date.parse(pending.expires_at)<=now)))return '审批已过期，请拒绝后重新准备。';
  if(operation.status!=='open'||operation.invalid_reason)return operation.invalid_reason?`${operation.invalid_reason}；请拒绝后重新准备。`:'审批已失效或已执行，请拒绝后重新准备。';
  return null;
}
