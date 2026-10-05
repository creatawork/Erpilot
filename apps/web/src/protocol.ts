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

export interface ToolFinishedPayload {
  id: string;
  name: string;
  content: string;
  ok: boolean;
}

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
  | { event: "tool_started"; data: ToolStartedPayload }
  | { event: "tool_finished"; data: ToolFinishedPayload }
  | { event: "approval_pending"; data: ApprovalPendingPayload }
  | { event: "approval_resolved"; data: ApprovalResolvedPayload }
  | { event: "done"; data: DonePayload }
  | { event: "error"; data: { message: string } };

function parseBlock(block: string): SSEEvent | null {
  let event: string | null = null;
  let data: string | null = null;
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data = line.slice(5).trim();
    // ": ping" 等注释行直接忽略
  }
  if (!event || !data) return null;
  return { event, data: JSON.parse(data) } as SSEEvent;
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
  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
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
  }
}
