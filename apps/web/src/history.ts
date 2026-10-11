import {ApiError} from './protocol.js';

const labels = {
  running: '执行中', waiting_approval: '待审批', recovering: '恢复中',
  reconciliation_required: '待核对', unknown: '状态待确认', completed: '已完成',
  failed: '执行失败', cancelled: '已取消',
};
export type HistoryStatus = keyof typeof labels;
export interface SessionSummary {session_id: string; title: string; updated_at: string; status: HistoryStatus}
export interface SessionPage {items: SessionSummary[]; has_more: boolean}
export function historyStatusLabel(status: HistoryStatus): string {return labels[status];}
export function parseSessionPage(value: unknown): SessionPage {
  if (!value || typeof value !== 'object' || !('items' in value) || !Array.isArray(value.items) ||
    !('has_more' in value) || typeof value.has_more !== 'boolean') throw new Error('历史会话格式无效');
  const items = value.items.map((item: unknown) => {
    if (!item || typeof item !== 'object' || !('session_id' in item) || typeof item.session_id !== 'string' || !item.session_id ||
      !('title' in item) || typeof item.title !== 'string' || !('updated_at' in item) || typeof item.updated_at !== 'string' ||
      !Number.isFinite(Date.parse(item.updated_at)) || !('status' in item) || typeof item.status !== 'string' ||
      !Object.hasOwn(labels, item.status)) throw new Error('历史会话格式无效');
    return item as SessionSummary;
  });
  return {items, has_more: value.has_more};
}
export async function loadSessionPage(offset = 0, signal?: AbortSignal): Promise<SessionPage> {
  const response = await fetch(`/api/sessions?limit=20&offset=${offset}`, {signal});
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, body?.error?.code, body?.error?.message ?? `HTTP ${response.status}`);
  }
  return parseSessionPage(await response.json());
}

interface RequestTicket {sessionId: string; generation: number; refresh?: number}
/** All session responses (including failures and deferred stream frames) share this boundary. */
export class SessionRequests {
  private generation = 0;
  private refreshGeneration = 0;
  constructor(private sessionId: string) {}
  switchTo(sessionId: string): void {this.sessionId = sessionId; this.generation++; this.refreshGeneration++;}
  capture(): RequestTicket {return {sessionId: this.sessionId, generation: this.generation};}
  refresh(): RequestTicket {return {...this.capture(), refresh: ++this.refreshGeneration};}
  isCurrent(ticket: RequestTicket): boolean {
    return ticket.sessionId === this.sessionId && ticket.generation === this.generation &&
      (ticket.refresh === undefined || ticket.refresh === this.refreshGeneration);
  }
}
