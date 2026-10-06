/** Durable run snapshots and SSE cursor protocol. Disconnecting never cancels a run. */
export type RunStatus = "running" | "waiting_approval" | "recovering" | "completed" | "failed" | "cancelled";
export interface UsagePayload { prompt_tokens: number; completion_tokens: number; total_tokens: number }
export interface DonePayload {
  steps: number; completed: boolean; usage: UsagePayload | null; cost: number | null;
  duration_ms: number; trace: string | null; status?: RunStatus;
}
export interface ToolStartedPayload { id: string; name: string; arguments: string }
export interface ToolFinishedPayload { id: string; name: string; content: string; ok: boolean; status?: string }
export interface ApprovalRecord {
  pending_id: string; invocation_id?: string; status: string; expected_version: number;
  arguments_fingerprint: string; expires_at: string; decided_reason?: string | null;
}
export interface ApprovalPendingPayload extends ApprovalRecord {
  call_id: string; tool: string; risk: string; arguments: Record<string, unknown>;
}
export interface ApprovalResolvedPayload {
  call_id: string; pending_id: string; tool: string; approved: boolean; reason: string;
}
type Envelope = { run_id: string; seq: number };
export type SSEEvent = (
  | { event: "start"; data: { session_id: string; model: string } }
  | { event: "step"; data: { step: number } }
  | { event: "delta"; data: { text: string } }
  | { event: "tool_started"; data: ToolStartedPayload }
  | { event: "tool_finished"; data: ToolFinishedPayload }
  | { event: "approval_pending"; data: ApprovalPendingPayload }
  | { event: "approval_resolved"; data: ApprovalResolvedPayload }
  | { event: "done"; data: DonePayload }
  | { event: "run_status"; data: { status: RunStatus; message?: string } }
  | { event: "error"; data: { message: string } }
) & { data: Envelope };
interface Invocation {
  invocation_id: string; call_id: string; tool: string; arguments: Record<string, unknown>;
  status: string; result: unknown; approval_pending_id: string;
}
interface Message {
  role: string; content?: string; tool_call_id?: string;
  tool_calls?: { id: string; function: { name: string; arguments: string } }[];
}
export interface RunSnapshot {
  run_id: string; session_id: string; user_input: string; status: RunStatus; messages: Message[];
  answer_text: string; final_answer: string | null; error: string | null; trace_path: string | null;
  invocations: Invocation[]; approvals: ApprovalRecord[]; pending: ApprovalRecord[];
  last_seq: number; events: SSEEvent[];
}
export interface ToolItem extends ToolStartedPayload {
  status: string; finished: ToolFinishedPayload | null;
  approval: ApprovalPendingPayload | null; approvalResolved: ApprovalResolvedPayload | null;
}
export interface RunView {
  runId: string; sessionId: string; userInput: string; status: RunStatus; text: string;
  tools: ToolItem[]; lastSeq: number; done?: DonePayload; error: string | null; trace: string | null;
}

export function isActiveRun(status: RunStatus): boolean {
  return status === "running" || status === "waiting_approval" || status === "recovering";
}

function upsertTool(run: RunView, id: string, update: (tool: ToolItem) => ToolItem): ToolItem[] {
  const found = run.tools.find(tool => tool.id === id);
  const next = update(found ?? { id, name: "", arguments: "", status: "running", finished: null,
    approval: null, approvalResolved: null });
  return found ? run.tools.map(tool => tool.id === id ? next : tool) : [...run.tools, next];
}

export function applyRunEvent(run: RunView, event: SSEEvent): RunView {
  if (event.data.run_id !== run.runId || event.data.seq <= run.lastSeq) return run;
  let next = { ...run, lastSeq: event.data.seq };
  switch (event.event) {
    case "delta": next.text += event.data.text; break;
    case "step": next.text = ""; break;
    case "tool_started":
      next.tools = upsertTool(next, event.data.id, tool => ({ ...tool, ...event.data })); break;
    case "tool_finished":
      next.tools = upsertTool(next, event.data.id, tool => ({ ...tool, name: event.data.name,
        status: event.data.status ?? (event.data.ok ? "succeeded" : "failed"), finished: event.data })); break;
    case "approval_pending":
      next.status = "waiting_approval";
      next.tools = upsertTool(next, event.data.call_id, tool => ({ ...tool, name: event.data.tool,
        arguments: JSON.stringify(event.data.arguments, null, 2),
        approval: { ...event.data, status: event.data.status ?? "pending" } })); break;
    case "approval_resolved":
      next.tools = upsertTool(next, event.data.call_id, tool => ({ ...tool,
        approval: tool.approval ? { ...tool.approval, status: event.data.approved ? "approved" : "denied" } : null,
        approvalResolved: event.data })); break;
    case "done": next.done = event.data; next.trace = event.data.trace;
      next.status = event.data.status ?? (event.data.completed ? "completed" : next.status); break;
    case "run_status": next.status = event.data.status; break;
    case "error": next.error = event.data.message; break;
  }
  return next;
}

export function restoreRun(snapshot: RunSnapshot): RunView {
  let run: RunView = { runId: snapshot.run_id, sessionId: snapshot.session_id,
    userInput: snapshot.user_input, status: snapshot.status, text: "", tools: [], lastSeq: 0,
    error: snapshot.error, trace: snapshot.trace_path };
  // Checkpoints preserve tool cards even if the stream never published their start.
  const userIndex = snapshot.messages.map(message => message.role).lastIndexOf("user");
  for (const message of snapshot.messages.slice(Math.max(userIndex, 0))) {
    for (const call of message.tool_calls ?? []) {
      run.tools = upsertTool(run, call.id, tool => ({ ...tool, name: call.function.name,
        arguments: call.function.arguments }));
    }
    if (message.role === "tool" && message.tool_call_id) {
      run.tools = upsertTool(run, message.tool_call_id, tool => ({ ...tool,
        finished: { id: tool.id, name: tool.name, content: message.content ?? "", ok: true } }));
    }
  }
  for (const event of snapshot.events) run = applyRunEvent(run, event);
  for (const invocation of snapshot.invocations) {
    const approval = snapshot.approvals.find(item => item.pending_id === invocation.approval_pending_id);
    run.tools = upsertTool(run, invocation.call_id, tool => ({ ...tool, name: invocation.tool,
      arguments: JSON.stringify(invocation.arguments, null, 2), status: invocation.status,
      finished: invocation.status === "succeeded" || invocation.status === "failed" || invocation.status === "denied"
        ? { id: invocation.call_id, name: invocation.tool,
          content: JSON.stringify(invocation.result, null, 2), ok: invocation.status === "succeeded" } : null,
      approval: approval ? { ...approval, call_id: invocation.call_id, tool: invocation.tool,
        risk: tool.approval?.risk ?? "写操作", arguments: invocation.arguments } : null,
      approvalResolved: approval && approval.status !== "pending" ? { call_id: invocation.call_id,
        pending_id: approval.pending_id, tool: invocation.tool, approved: approval.status === "approved",
        reason: approval.decided_reason ?? approval.status } : null,
    }));
  }
  return { ...run, text: snapshot.final_answer ?? snapshot.answer_text, status: snapshot.status,
    lastSeq: snapshot.last_seq, error: snapshot.error, trace: snapshot.trace_path };
}

export function canDecideApproval(approval: ApprovalRecord, now = Date.now()): boolean {
  return approval.status === "pending" && Date.parse(approval.expires_at) > now;
}

/** Cancelled tasks may still have a write whose commit result needs reconciliation. */
export function needsResultVerification(run: RunView): boolean {
  return run.status === "cancelled" && run.tools.some(
    tool => tool.status === "unknown" || tool.status === "executing",
  );
}

/** Read durable state without resuming the model or retrying a business write. */
export async function verifyCancelledRun(run: RunView): Promise<RunView> {
  if (!needsResultVerification(run)) return run;
  return loadRun(run.runId);
}

export function runNotice(run: RunView): string {
  const unknown = run.tools.some(tool => tool.status === "unknown" || tool.status === "executing");
  const committed = run.tools.some(tool => tool.approval && tool.status === "succeeded");
  if (unknown) return "结果待核对，请重试恢复以查询真实业务结果。";
  if (run.status === "cancelled") return "任务已取消；已执行业务未撤销。";
  if (committed && run.status !== "completed") return "业务已执行，回答尚未完成；恢复将核对原结果。";
  if (run.status === "waiting_approval") return "待审批";
  if (run.status === "recovering") return "正在恢复任务，等待核对与回答。";
  if (run.status === "failed") return "任务失败；请查看错误后发起新的消息。";
  return run.status === "running" ? "生成中…" : "";
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public approval?: ApprovalRecord) { super(message); }
}

async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new ApiError(response.status,
      typeof detail === "string" ? detail : detail?.message ?? `请求失败：HTTP ${response.status}`,
      typeof detail === "object" ? detail?.approval : undefined);
  }
  return response.json() as Promise<T>;
}

type ApprovalBinding = Pick<RunSnapshot, "run_id" | "session_id"> &
  Pick<ApprovalRecord, "expected_version" | "arguments_fingerprint">;
export async function submitApproval(pendingId: string, approved: boolean, binding?: ApprovalBinding): Promise<void> {
  const result = await responseJson<{ ok: boolean }>(await fetch("/api/chat/approve", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ pending_id: pendingId, approved, ...binding }),
  }));
  if (result.ok !== true) throw new Error("审批请求已失效或已处理，请刷新任务状态。");
}

export async function loadSessionRuns(sessionId: string, signal?: AbortSignal): Promise<RunView[]> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, { signal });
  if (response.status === 404) return [];
  const body = await responseJson<{ runs: RunSnapshot[] }>(response);
  return body.runs.map(restoreRun);
}

export async function loadRun(runId: string, signal?: AbortSignal): Promise<RunView> {
  return restoreRun(await responseJson<RunSnapshot>(await fetch(`/api/runs/${encodeURIComponent(runId)}`, { signal })));
}

export async function createRun(message: string, sessionId: string): Promise<RunView> {
  return restoreRun(await responseJson<RunSnapshot>(await fetch("/api/runs", { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ message, session_id: sessionId }) })));
}

export async function cancelRun(runId: string): Promise<RunView> {
  return restoreRun(await responseJson<RunSnapshot>(await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" })));
}

function parseBlock(block: string): SSEEvent | null {
  let event = "";
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  return event && data.length ? { event, data: JSON.parse(data.join("\n")) } as SSEEvent : null;
}

async function* readEvents(response: Response): AsyncGenerator<SSEEvent> {
  if (!response.ok) await responseJson(response);
  if (!response.body) throw new Error("事件订阅没有响应内容");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let sep = buffer.match(/\r?\n\r?\n/);
      while (sep?.index !== undefined) {
        const block = buffer.slice(0, sep.index);
        buffer = buffer.slice(sep.index + sep[0].length);
        const parsed = parseBlock(block);
        if (parsed) yield parsed;
        sep = buffer.match(/\r?\n\r?\n/);
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

/** One recovery attempt. Callers back off before retrying a broken subscription. */
export async function* recoverRun(runId: string, signal?: AbortSignal): AsyncGenerator<RunView> {
  let run = await loadRun(runId, signal);
  yield run;
  if (!isActiveRun(run.status)) return;
  run = restoreRun(await responseJson<RunSnapshot>(await fetch(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST", signal,
  })));
  yield run;
  for await (const event of readEvents(await fetch(`/api/runs/${encodeURIComponent(runId)}/events?after_seq=${run.lastSeq}`, { signal }))) {
    run = applyRunEvent(run, event);
    yield run;
  }
  // An orderly close can still leave an active or unknown task. Read durable truth.
  yield await loadRun(runId, signal);
}

export type RecoveryUpdate = { kind: "run"; run: RunView } |
  { kind: "disconnected"; message: string; retrying: boolean };

function waitForReconnect(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal?.reason); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, milliseconds);
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function* followRun(runId: string, signal?: AbortSignal): AsyncGenerator<RecoveryUpdate> {
  let failures = 0;
  while (!signal?.aborted) {
    try {
      let latest: RunView | undefined;
      for await (const run of recoverRun(runId, signal)) {
        latest = run;
        yield { kind: "run", run };
      }
      if (!latest || !isActiveRun(latest.status) || latest.tools.some(tool => tool.status === "unknown")) return;
    } catch (error) {
      if (signal?.aborted) return;
      const retrying = !(error instanceof ApiError && error.status >= 400 && error.status < 500);
      yield { kind: "disconnected", message: error instanceof Error ? error.message : String(error), retrying };
      if (!retrying) return;
    }
    // Includes premature orderly closes: never turn these into a tight resume loop.
    await waitForReconnect(Math.min(1000 * 2 ** Math.min(failures++, 4), 15000), signal).catch(error => {
      if (!signal?.aborted) throw error;
    });
  }
}

/** Legacy consumers can still use this endpoint; the UI uses durable create/resume. */
export async function* streamChat(message: string, sessionId: string, signal?: AbortSignal): AsyncGenerator<SSEEvent> {
  yield* readEvents(await fetch("/api/chat/stream", { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ message, session_id: sessionId }), signal }));
}
