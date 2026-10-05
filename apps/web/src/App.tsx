import { useCallback, useRef, useState } from "react";
import "./App.css";
import {
  type ApprovalPendingPayload,
  type ApprovalResolvedPayload,
  type DonePayload,
  type ToolFinishedPayload,
  type ToolStartedPayload,
  streamChat,
  submitApproval,
} from "./protocol";

interface ToolItem {
  id: string;
  name: string;
  arguments: string;
  finished: ToolFinishedPayload | null;
  approval: ApprovalPendingPayload | null;
  approvalResolved: ApprovalResolvedPayload | null;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolItem[];
  done?: DonePayload;
}

function newSessionId(): string {
  return crypto.randomUUID?.() ?? Math.random().toString(36).slice(2);
}

function sessionKey(): string {
  const KEY = "erpilot-session-id";
  let id = localStorage.getItem(KEY);
  if (!id) {
    id = newSessionId();
    localStorage.setItem(KEY, id);
  }
  return id;
}

export default function App() {
  const sessionIdRef = useRef<string>(sessionKey());
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [submittingApprovals, setSubmittingApprovals] = useState<Set<string>>(new Set());
  const approvalInFlight = useRef(new Set<string>());
  const listRef = useRef<HTMLDivElement>(null);

  const scrollToEnd = () => {
    requestAnimationFrame(() => {
      listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
    });
  };

  const respondApproval = useCallback(
    async (pendingId: string, approved: boolean) => {
      if (approvalInFlight.current.has(pendingId)) return;
      approvalInFlight.current.add(pendingId);
      setSubmittingApprovals(new Set(approvalInFlight.current));
      setError("");
      try {
        await submitApproval(pendingId, approved);
      } catch (e) {
        setError(e instanceof Error ? e.message : "审批提交失败，请重试。");
      } finally {
        approvalInFlight.current.delete(pendingId);
        setSubmittingApprovals(new Set(approvalInFlight.current));
      }
    },
    [],
  );

  const send = useCallback(async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setError("");
    setBusy(true);
    setTurns(prev => [
      ...prev,
      { role: "user", text: message, tools: [] },
      { role: "assistant", text: "", tools: [] },
    ]);
    scrollToEnd();

    const updateAssistant = (fn: (turn: Turn) => Turn) => {
      setTurns(prev => {
        const next = [...prev];
        const i = next.map(t => t.role).lastIndexOf("assistant");
        if (i >= 0) next[i] = fn(next[i]);
        return next;
      });
      scrollToEnd();
    };

    try {
      for await (const ev of streamChat(message, sessionIdRef.current)) {
        switch (ev.event) {
          case "start":
            setStatus(`${ev.data.model} · 会话 ${ev.data.session_id.slice(0, 8)}`);
            break;
          case "step":
            setStatus(`第 ${ev.data.step} 轮思考中…`);
            break;
          case "delta":
            updateAssistant(t => ({ ...t, text: t.text + ev.data.text }));
            break;
          case "tool_started": {
            const started: ToolStartedPayload = ev.data;
            updateAssistant(t => ({
              ...t,
              tools: [
                ...t.tools,
                { ...started, finished: null, approval: null, approvalResolved: null },
              ],
            }));
            setStatus(`调用工具 ${ev.data.name}…`);
            break;
          }
          case "tool_finished": {
            const finished: ToolFinishedPayload = ev.data;
            updateAssistant(t => ({
              ...t,
              tools: t.tools.map(x =>
                x.id === finished.id ? { ...x, finished } : x,
              ),
            }));
            break;
          }
          case "approval_pending": {
            const pending: ApprovalPendingPayload = ev.data;
            updateAssistant(t => ({
              ...t,
              tools: t.tools.map(x =>
                x.id === pending.call_id ? { ...x, approval: pending } : x,
              ),
            }));
            setStatus(`待审批：${pending.tool}（${pending.risk}）`);
            break;
          }
          case "approval_resolved": {
            const resolved: ApprovalResolvedPayload = ev.data;
            updateAssistant(t => ({
              ...t,
              tools: t.tools.map(x =>
                x.id === resolved.call_id ? { ...x, approvalResolved: resolved } : x,
              ),
            }));
            setStatus(resolved.approved ? "已批准，执行中…" : "已拒绝");
            break;
          }
          case "done":
            updateAssistant(t => ({ ...t, done: ev.data }));
            setStatus("");
            break;
          case "error":
            setError(ev.data.message);
            setStatus("");
            break;
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }, [input, busy]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="app">
      <header>
        <h1>Erpilot 掌柜助手</h1>
        <span className="status">{status || (busy ? "生成中…" : "")}</span>
      </header>

      <div className="chat" ref={listRef}>
        {turns.length === 0 && (
          <div className="empty">
            试试问：「订单 123 里买了什么？还有货吗？有货的话报个价」
          </div>
        )}
        {turns.map((turn, i) =>
          turn.role === "user" ? (
            <div className="bubble user" key={i}>
              {turn.text}
            </div>
          ) : (
            <div className="bubble assistant" key={i}>
              {turn.tools.length > 0 && (
                <div className="tools">
                  {turn.tools.map(tool => (
                    <details key={tool.id} open={!tool.finished}>
                      <summary>
                        <span className={tool.finished ? (tool.finished.ok ? "ok" : "fail") : "running"}>
                          {tool.finished ? (tool.finished.ok ? "✓" : "✗") : "⋯"}
                        </span>{" "}
                        {tool.name}
                      </summary>
                      <pre className="args">{tool.arguments}</pre>
                      {tool.approval && !tool.approvalResolved && (
                        <div className="approval">
                          <span className="approval-label">
                            待审批 · 风险等级 {tool.approval.risk}
                          </span>
                          <button
                            className="approve"
                            disabled={submittingApprovals.has(tool.approval.pending_id)}
                            onClick={() => void respondApproval(tool.approval!.pending_id, true)}
                          >
                            批准
                          </button>
                          <button
                            className="deny"
                            disabled={submittingApprovals.has(tool.approval.pending_id)}
                            onClick={() => void respondApproval(tool.approval!.pending_id, false)}
                          >
                            拒绝
                          </button>
                        </div>
                      )}
                      {tool.approvalResolved && (
                        <div className={`approval ${tool.approvalResolved.approved ? "ok" : "fail"}`}>
                          {tool.approvalResolved.approved
                            ? "已批准"
                            : `已拒绝：${tool.approvalResolved.reason || "无理由"}`}
                        </div>
                      )}
                      {tool.finished && <pre className="result">{tool.finished.content}</pre>}
                    </details>
                  ))}
                </div>
              )}
              <div className="text">
                {turn.text}
                {!turn.done && !turn.text && busy && <span className="cursor">▍</span>}
              </div>
              {turn.done && (
                <div className="meta">
                  {turn.done.completed ? "" : "（触发 max_steps 防护）"}
                  {turn.done.usage &&
                    ` ${turn.done.usage.prompt_tokens}+${turn.done.usage.completion_tokens} tok`}
                  {turn.done.cost != null && ` ≈¥${turn.done.cost.toFixed(4)}`}
                  {" · "}
                  {(turn.done.duration_ms / 1000).toFixed(1)}s
                </div>
              )}
            </div>
          ),
        )}
        {error && <div className="error">{error}</div>}
      </div>

      <footer>
        <textarea
          value={input}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={2}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
        />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  );
}
