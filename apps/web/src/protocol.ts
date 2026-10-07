/**
 * SSE 事件协议 v1 的前端镜像——与 apps/api/src/erpilot_api/events.py 必须同步修改。
 * streamChat() 用 fetch + ReadableStream 手解 SSE（EventSource 不支持 POST）。
 */

export interface StartPayload {
  session_id: string;
  model: string;
}

export interface ToolStartedPayload {
  id: string;
  name: string;
  arguments: string;
}

export interface ToolExecutingPayload {
  id: string;
  name: string;
}

export interface ToolFinishedPayload {
  id: string;
  name: string;
  content: string;
  ok: boolean;
  /** 展示适配器产物（设计 5.2）；未知工具/契约不符/版本不识别时为 null 或未知 kind */
  display?: BusinessDisplay | null;
}

/** 后端展示适配器协议（apps/api presentation / agent_core display 同步维护） */
export type BusinessOutcome = "succeeded" | "failed" | "denied" | "unknown";

export interface BusinessDisplayBase {
  version: number;
  outcome: BusinessOutcome;
  sku?: string;
  message?: string;
  error_code?: string;
}

export interface StockQueryDisplay extends BusinessDisplayBase {
  kind: "stock_query";
  quantity: number;
  available?: boolean;
}

export interface StockAdjustmentDisplay extends BusinessDisplayBase {
  kind: "stock_adjustment";
  delta: number;
  quantity_after: number;
}

export interface OrderCreationDisplay extends BusinessDisplayBase {
  kind: "order_creation";
  order_id: string;
  status?: string;
  customer?: string;
  total_amount?: number;
}

export type BusinessDisplay =
  | StockQueryDisplay
  | StockAdjustmentDisplay
  | OrderCreationDisplay
  | (BusinessDisplayBase & { kind: string; [key: string]: unknown });

export interface UsagePayload {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ApprovalPendingPayload {
  call_id: string;
  pending_id: string;
  tool: string;
  risk: string;
  arguments: Record<string, unknown>;
}

export interface ApprovalResolvedPayload {
  call_id: string;
  pending_id: string;
  tool: string;
  approved: boolean;
  reason: string;
}

export interface DonePayload {
  steps: number;
  completed: boolean;
  usage: UsagePayload | null;
  cost: number | null;
  duration_ms: number;
  trace: string | null;
}

export type SSEEvent =
  | { event: "start"; data: StartPayload }
  | { event: "step"; data: { step: number } }
  | { event: "delta"; data: { text: string } }
  | { event: "reasoning_delta"; data: { step: number; text: string } }
  | { event: "tool_started"; data: ToolStartedPayload }
  | { event: "tool_executing"; data: ToolExecutingPayload }
  | { event: "tool_finished"; data: ToolFinishedPayload }
  | { event: "approval_pending"; data: ApprovalPendingPayload }
  | { event: "approval_resolved"; data: ApprovalResolvedPayload }
  | { event: "done"; data: DonePayload }
  | { event: "heartbeat"; data: { at: string } }
  | { event: "error"; data: { code?: string; message: string } };

const KNOWN_EVENTS = new Set([
  "start", "step", "delta", "reasoning_delta", "tool_started", "tool_executing",
  "tool_finished", "approval_pending", "approval_resolved", "done", "heartbeat", "error",
]);

/** 已知事件的 data 不是合法 JSON：按可解释的协议错误处理，不静默丢弃。 */
export class ProtocolError extends Error {
  constructor(readonly eventName: string, cause: unknown) {
    super(`事件 ${eventName} 的数据无法解析（协议错误）`);
    this.name = "ProtocolError";
    this.cause = cause;
  }
}

export interface SessionMessage {
  role: string;
  content?: string | null;
  tool_call_id?: string;
  tool_calls?: { id: string; function: { name: string; arguments: string } }[];
}

export interface SessionToolResult {
  call_id: string;
  name: string;
  content: string;
  ok: boolean;
  display?: BusinessDisplay | null;
}

export interface SessionState {
  session_id: string;
  status: "completed" | "waiting_approval" | "running" | "interrupted";
  messages: SessionMessage[];
  pending_approvals: ApprovalPendingPayload[];
  tool_results: SessionToolResult[];
  /** 展示投影（设计 5.3）：可选，旧快照没有；新客户端优先使用，缺失时走 messages 转换 */
  presentation?: PresentationSnapshot;
}

/** 展示事件回放产物（apps/api/presentation.py 同步维护） */
export interface PresentationTool {
  name: string;
  arguments: string;
  status: "prepared" | "waiting_approval" | "running" | "succeeded" | "failed" | "denied" | "unknown";
  finished: ToolFinishedPayload | null;
  approval: ApprovalPendingPayload | null;
  approvalResolved: ApprovalResolvedPayload | null;
}

export interface PresentationTurn {
  run_id: string;
  user_index?: number | null;
  user_text: string;
  blocks: ({ type: "text"; text: string } | { type: "tool"; callId: string })[];
  tools: Record<string, PresentationTool>;
  done: DonePayload | null;
  error: string | null;
  terminal: boolean;
}

export interface PresentationSnapshot {
  version: number;
  turns: PresentationTurn[];
  last_seq: number;
  updated_at: string | null;
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string | undefined, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function responseError(resp: Response): Promise<ApiError> {
  const body = await resp.json().catch(() => null);
  return new ApiError(resp.status, body?.error?.code, body?.error?.message ?? `HTTP ${resp.status}`);
}

export async function loadSessionState(
  sessionId: string, signal?: AbortSignal, allowMissing = false,
): Promise<SessionState | null> {
  const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/state`, { signal });
  if (resp.status === 404 && allowMissing) return null;
  if (!resp.ok) throw await responseError(resp);
  const data: unknown = await resp.json();
  if (!data || typeof data !== "object" || !("messages" in data) ||
      !("pending_approvals" in data) || !Array.isArray(data.messages) ||
      !Array.isArray(data.pending_approvals) || !("tool_results" in data) ||
      !Array.isArray(data.tool_results)) throw new Error("会话状态格式无效");
  return data as SessionState;
}

export async function* streamApprovalResume(
  sessionId: string, pendingId: string, approved: boolean, reason = "", signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const resp = await fetch("/api/chat/approve/stream", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, pending_id: pendingId, approved, reason }), signal,
  });
  yield* readSSE(resp);
}

export async function* streamInterruptedResume(sessionId: string): AsyncGenerator<SSEEvent> {
  const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/resume/stream`, {
    method: "POST",
  });
  yield* readSSE(resp);
}

export async function submitApproval(pendingId: string, approved: boolean): Promise<void> {
  const resp = await fetch("/api/chat/approve", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pending_id: pendingId, approved }),
  });
  if (!resp.ok) throw await responseError(resp);
  const result: unknown = await resp.json();
  if (!result || typeof result !== "object" || !("ok" in result) || result.ok !== true) {
    throw new ApiError(409, "stale_approval", "审批请求已失效或已处理，请重新发起操作。");
  }
}

function parseBlock(block: string): SSEEvent | null {
  let event: string | null = null;
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    // ": ping" 等注释行直接忽略（心跳不代表业务进度）
  }
  if (!event || dataLines.length === 0) return null;
  // 多行 data 按 SSE 规范以 \n 拼接；JSON 数据通常单行，拼接后兼容分片写入
  const raw = dataLines.join("\n");
  if (!KNOWN_EVENTS.has(event)) {
    // 新增的可选事件不得让旧页面崩溃：忽略并留诊断
    console.warn(`[erpilot] 收到未知事件 ${event}，已忽略`);
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch (err) {
    throw new ProtocolError(event, err);
  }
  return { event, data } as SSEEvent;
}

export async function* streamChat(
  message: string,
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const resp = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  yield* readSSE(resp);
}

async function* readSSE(resp: Response): AsyncGenerator<SSEEvent> {
  if (!resp.ok) throw await responseError(resp);
  if (!resp.body) throw new Error("响应缺少事件流");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try { while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 事件块以空行分隔（\n\n 或 \r\n\r\n）
    let sep = buffer.match(/\r?\n\r?\n/);
    while (sep && sep.index !== undefined) {
      const block = buffer.slice(0, sep.index);
      buffer = buffer.slice(sep.index + sep[0].length);
      const parsed = parseBlock(block);
      if (parsed) yield parsed;
      sep = buffer.match(/\r?\n\r?\n/);
    }
  } } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
