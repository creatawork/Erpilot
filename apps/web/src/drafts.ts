export interface DraftLine { sku: string; name: string; quantity: number; base_price: number; discount: number; unit_price: number; line_total: number; stock_before: number; stock_after: number }
export interface OrderDraft { draft_id: string; revision: number; customer: string; note: string | null; items: DraftLine[]; total_amount: number; expires_at: string; status: 'open' | 'expired' | 'superseded' | 'submitted'; order_id: string | null }
export interface DraftInput { customer: string; items: {sku: string; quantity: number}[]; note?: string }
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0;
const integer = (v: unknown) => number(v) && Number.isSafeInteger(v);
export function parseOrderDraft(value: unknown): OrderDraft {
  if (!record(value) || typeof value.draft_id !== 'string' || !value.draft_id || !integer(value.revision) || Number(value.revision) < 1 || typeof value.customer !== 'string' || !(value.note === null || typeof value.note === 'string') || !number(value.total_amount) || typeof value.expires_at !== 'string' || !Number.isFinite(Date.parse(value.expires_at)) || !['open','expired','superseded','submitted'].includes(String(value.status)) || !(value.order_id === null || typeof value.order_id === 'string') || !Array.isArray(value.items) || !value.items.length) throw new Error('订单草稿格式无效');
  for (const line of value.items) {
    if (!record(line) || typeof line.sku !== 'string' || !line.sku || typeof line.name !== 'string' || !integer(line.quantity) || Number(line.quantity) < 1 || !['base_price','discount','unit_price','line_total'].every(key => number(line[key])) || !integer(line.stock_before) || !integer(line.stock_after)) throw new Error('订单草稿明细格式无效');
  }
  return value as unknown as OrderDraft;
}
function base(sessionId: string) { return `/api/sessions/${encodeURIComponent(sessionId)}/order-drafts`; }
async function request(url: string, options?: RequestInit): Promise<unknown> {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = record(body) && record(body.error) ? body.error : {};
    throw Object.assign(new Error([detail.message ?? `HTTP ${response.status}`, detail.hint].filter(Boolean).join('；')), {status:response.status, code:detail.code});
  }
  return body;
}
export async function listOrderDrafts(sessionId: string, signal?: AbortSignal): Promise<OrderDraft[]> {
  const body = await request(base(sessionId), {signal});
  if (!record(body) || !Array.isArray(body.items)) throw new Error('订单草稿列表格式无效');
  return body.items.map(parseOrderDraft);
}
export async function getOrderDraft(sessionId: string, draftId: string, revision?: number, signal?: AbortSignal) {
  return parseOrderDraft(await request(`${base(sessionId)}/${encodeURIComponent(draftId)}${revision === undefined ? '' : `?revision=${revision}`}`, {signal}));
}
export async function createOrderDraft(sessionId: string, input: DraftInput) {
  return parseOrderDraft(await request(base(sessionId), {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(input)}));
}
export async function reviseOrderDraft(sessionId: string, draftId: string, revision: number, input: DraftInput) {
  return parseOrderDraft(await request(`${base(sessionId)}/${encodeURIComponent(draftId)}`, {method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({...input,expected_revision:revision})}));
}
export function approvalDraftIssue(pending: {arguments: Record<string, unknown>}, snapshot: OrderDraft | undefined, latest: OrderDraft[], now = Date.now()): string | null {
  if (!snapshot) return '正在核对审批明细；无法核对时请拒绝后重新提交。';
  if (snapshot.draft_id !== pending.arguments.draft_id || snapshot.revision !== pending.arguments.revision) return '审批版本不匹配，请拒绝后重新提交。';
  const current = latest.find(item => item.draft_id === snapshot.draft_id);
  if (!current) return '当前草稿未核对，请刷新或拒绝后重新提交。';
  if (current.revision !== snapshot.revision) return '草稿已修改，请拒绝旧审批后重新提交。';
  if (current.status !== 'open' || Date.parse(snapshot.expires_at) <= now) return '草稿已过期或已提交，请拒绝旧审批后刷新。';
  return null;
}
