import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import {
  ApiError, type RunView, type ToolItem, canDecideApproval, cancelRun, createRun, followRun,
  isActiveRun, loadRun, loadSessionRuns, needsResultVerification, runNotice, submitApproval,
  verifyCancelledRun,
} from "./protocol";

function sessionKey(): string {
  const key = "erpilot-session-id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = crypto.randomUUID?.() ?? Math.random().toString(36).slice(2);
    localStorage.setItem(key, id);
  }
  return id;
}

function approvalLabel(tool: ToolItem, now: number): string {
  const approval = tool.approval;
  if (!approval) return "";
  if (approval.status === "pending") return canDecideApproval(approval, now)
    ? `待审批 · 风险等级 ${approval.risk}` : "审批已过期，请刷新任务状态。";
  if (approval.status === "approved") return "已批准";
  if (approval.status === "expired") return "审批已过期";
  if (approval.status === "cancelled") return "审批已取消";
  return `已拒绝：${approval.decided_reason || tool.approvalResolved?.reason || "无理由"}`;
}

function ToolCard({ tool, now, submitting, onDecision }: {
  tool: ToolItem; now: number; submitting: boolean; onDecision: (approved: boolean) => void;
}) {
  const uncertain = tool.status === "unknown" || tool.status === "executing";
  const finished = uncertain ? null : tool.finished;
  const actionable = tool.approval && canDecideApproval(tool.approval, now);
  return (
    <details open={!finished}>
      <summary>
        <span className={finished ? (finished.ok ? "ok" : "fail") : "running"}>
          {finished ? (finished.ok ? "✓" : "✗") : "⋯"}
        </span>{" "}{tool.name}{uncertain && " · 结果待核对"}
      </summary>
      <pre className="args">{tool.arguments}</pre>
      {tool.approval && (
        <div className={`approval ${tool.approval.status === "approved" ? "ok" : ""}`}>
          <span className="approval-label">{approvalLabel(tool, now)}</span>
          {actionable && <>
            <button className="approve" disabled={submitting} onClick={() => onDecision(true)}>
              {submitting ? "提交中…" : "批准"}
            </button>
            <button className="deny" disabled={submitting} onClick={() => onDecision(false)}>拒绝</button>
          </>}
          <span className="approval-expiry">截止 {new Date(tool.approval.expires_at).toLocaleString()}</span>
        </div>
      )}
      {finished && <pre className="result">{finished.content}</pre>}
    </details>
  );
}

export default function App() {
  const [sessionId] = useState(sessionKey);
  const [runs, setRuns] = useState<RunView[]>([]);
  const [input, setInput] = useState("");
  const [actionError, setActionError] = useState("");
  const [connection, setConnection] = useState("");
  const [restoring, setRestoring] = useState(true);
  const [historyBlocked, setHistoryBlocked] = useState(false);
  const [creating, setCreating] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [recoveryEpoch, setRecoveryEpoch] = useState(0);
  const [now, setNow] = useState(Date.now);
  const [submittingApprovals, setSubmittingApprovals] = useState<Set<string>>(new Set());
  const approvalInFlight = useRef(new Set<string>());
  const createInFlight = useRef(false);
  const listRef = useRef<HTMLDivElement>(null);
  const active = runs.find(run => isActiveRun(run.status));
  const busy = restoring || historyBlocked || creating || Boolean(active);

  const saveRun = useCallback((run: RunView) => {
    setRuns(previous => {
      const existing = previous.find(item => item.runId === run.runId);
      // An approval refresh can race an event already received by the subscription.
      if (existing && existing.lastSeq > run.lastSeq) return previous;
      return existing ? previous.map(item => item.runId === run.runId ? run : item) : [...previous, run];
    });
  }, []);

  const retryRecovery = useCallback(() => {
    setRestoring(true);
    setRecoveryEpoch(epoch => epoch + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const restore = async () => {
      setRestoring(true);
      setConnection("正在找回会话…");
      try {
        const snapshots = await loadSessionRuns(sessionId, controller.signal);
        if (controller.signal.aborted) return;
        setRuns(snapshots);
        setHistoryBlocked(false);
        setRestoring(false);
        setConnection("");
        const existing = snapshots.find(run => isActiveRun(run.status));
        if (!existing) return;
        for await (const update of followRun(existing.runId, controller.signal)) {
          if (controller.signal.aborted) return;
          if (update.kind === "run") {
            saveRun(update.run);
            setConnection("");
          } else {
            setConnection(`${update.message}；${update.retrying ? "连接中断，正在自动重连。" : "请核对后重试恢复。"}`);
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setRestoring(false);
        setHistoryBlocked(true);
        setConnection(`${error instanceof Error ? error.message : String(error)}；暂未确认任务状态，请重试恢复。`);
        if (!(error instanceof ApiError && error.status >= 400 && error.status < 500)) {
          retryTimer = setTimeout(() => setRecoveryEpoch(epoch => epoch + 1), 5000);
        }
      }
    };
    void restore();
    return () => { controller.abort(); clearTimeout(retryTimer); };
  }, [sessionId, recoveryEpoch, saveRun]);

  useEffect(() => {
    window.addEventListener("online", retryRecovery);
    return () => window.removeEventListener("online", retryRecovery);
  }, [retryRecovery]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [runs, actionError]);

  const respondApproval = async (run: RunView, tool: ToolItem, approved: boolean) => {
    const approval = tool.approval;
    if (!approval || !canDecideApproval(approval) || approvalInFlight.current.has(approval.pending_id)) return;
    approvalInFlight.current.add(approval.pending_id);
    setSubmittingApprovals(new Set(approvalInFlight.current));
    setActionError("");
    try {
      await submitApproval(approval.pending_id, approved, { run_id: run.runId, session_id: run.sessionId,
        expected_version: approval.expected_version, arguments_fingerprint: approval.arguments_fingerprint });
      try { saveRun(await loadRun(run.runId)); }
      catch { setActionError("审批已提交，正在重新核对任务状态。"); retryRecovery(); }
    } catch (error) {
      setActionError(`${error instanceof Error ? error.message : "审批提交失败"}；请核对卡片状态后重试。`);
      if (error instanceof ApiError && [404, 409, 410].includes(error.status)) {
        try { saveRun(await loadRun(run.runId)); }
        catch { setConnection("审批状态刷新失败，请重试恢复。"); }
      }
    } finally {
      approvalInFlight.current.delete(approval.pending_id);
      setSubmittingApprovals(new Set(approvalInFlight.current));
    }
  };

  const send = async () => {
    const message = input.trim();
    if (!message || busy || createInFlight.current) return;
    createInFlight.current = true;
    setCreating(true);
    setActionError("");
    try {
      saveRun(await createRun(message, sessionId));
      setInput("");
    } catch (error) {
      setActionError(`${error instanceof Error ? error.message : String(error)}；正在查询本次请求的任务状态。`);
      // The POST may have committed before its response was lost. Query; never repost automatically.
    } finally {
      createInFlight.current = false;
      setCreating(false);
      retryRecovery();
    }
  };

  const cancel = async () => {
    if (!active || cancelling) return;
    setCancelling(true);
    setActionError("");
    try { saveRun(await cancelRun(active.runId)); }
    catch (error) { setActionError(`${error instanceof Error ? error.message : "取消失败"}；请重试恢复核对任务状态。`); }
    finally { setCancelling(false); retryRecovery(); }
  };

  const verifyCancelled = async (run: RunView) => {
    try { saveRun(await verifyCancelledRun(run)); setActionError(""); }
    catch (error) { setActionError(`${error instanceof Error ? error.message : "核对失败"}；可重试读取任务状态。`); }
  };

  return (
    <div className="app">
      <header>
        <h1>Erpilot 掌柜助手</h1>
        <span className="status" role="status">{restoring ? "正在恢复…" : active ? runNotice(active) : ""}</span>
      </header>
      {(connection || active) && (
        <div className="recovery-bar">
          {connection && <span role="status">{connection}</span>}
          <button className="secondary" onClick={retryRecovery} disabled={restoring}>重试恢复</button>
          {active && <button className="secondary" onClick={() => void cancel()} disabled={cancelling}>
            {cancelling ? "取消中…" : "取消任务"}
          </button>}
          {active && <span className="cancel-note">取消会停止后续任务；已执行业务不会撤销。</span>}
        </div>
      )}
      <div className="chat" ref={listRef} aria-busy={restoring}>
        {runs.length === 0 && !restoring && !historyBlocked && <div className="empty">
          试试问：「订单 123 里买了什么？还有货吗？有货的话报个价」
        </div>}
        {runs.map(run => <div className="turn" key={run.runId}>
          <div className="bubble user">{run.userInput}</div>
          <div className="bubble assistant">
            {run.tools.length > 0 && <div className="tools">{run.tools.map(tool => (
              <ToolCard key={tool.id} tool={tool} now={now}
                submitting={Boolean(tool.approval && submittingApprovals.has(tool.approval.pending_id))}
                onDecision={approved => void respondApproval(run, tool, approved)} />
            ))}</div>}
            <div className="text">
              {run.tools.some(tool => tool.status === "unknown") ? "当前业务结果待核对，回答将在核对后确认。" : run.text}
              {!run.text && run.status === "running" && <span className="cursor">▍</span>}
            </div>
            {runNotice(run) && <div className="run-notice" role="status">{runNotice(run)}</div>}
            {needsResultVerification(run) && <button className="secondary" onClick={() => void verifyCancelled(run)}>
              重新核对业务结果
            </button>}
            {run.error && <div className="error" role="alert">{run.error}</div>}
            {run.done && <div className="meta">
              {!run.done.completed && run.status === "completed" ? "（触发 max_steps 防护）" : ""}
              {run.done.usage && ` ${run.done.usage.prompt_tokens}+${run.done.usage.completion_tokens} tok`}
              {run.done.cost != null && ` ≈¥${run.done.cost.toFixed(4)}`} · {(run.done.duration_ms / 1000).toFixed(1)}s
            </div>}
            {run.trace && <div className="meta">任务 {run.runId.slice(0, 8)} · Trace：{run.trace}</div>}
          </div>
        </div>)}
        {actionError && <div className="error" role="alert">{actionError}</div>}
      </div>
      <footer>
        <textarea value={input} aria-label="消息" placeholder="输入消息，Enter 发送，Shift+Enter 换行" rows={2}
          onChange={event => setInput(event.target.value)} disabled={busy}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault(); void send();
          } }} />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>发送</button>
      </footer>
    </div>
  );
}
