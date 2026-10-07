import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import {
  type ApprovalPendingPayload,
  type SSEEvent,
  type SessionState,
  ApiError,
  loadSessionState,
  streamInterruptedResume,
  streamApprovalResume,
  streamChat,
  submitApproval,
} from "./protocol";

import {
  type AssistantTurn,
  type RestoreState,
  type Turn,
  applyTurnEvent,
  canSendMessage,
  hydrateTurns,
  isAssistantTurn,
  isTerminalPhase,
  markDisconnected,
  newAssistantTurn,
} from "./session";
import { AssistantMessage } from "./components/AssistantMessage";

const SESSION_KEY = "erpilot-session-id";
const FRESH_SESSION_KEY = "erpilot-fresh-session-id";

function newSessionId(): string {
  return crypto.randomUUID?.() ?? Math.random().toString(36).slice(2);
}

function sessionKey(): string {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = newSessionId();
    localStorage.setItem(SESSION_KEY, id);
    localStorage.setItem(FRESH_SESSION_KEY, id);
  }
  return id;
}

function isFreshSession(sessionId: string): boolean {
  return localStorage.getItem(FRESH_SESSION_KEY) === sessionId;
}

function isConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && (
    error.status === 409 || ["unfinished_run", "session_busy", "stale_approval"].includes(error.code ?? "")
  );
}

export default function App() {
  const sessionIdRef = useRef<string>(sessionKey());
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string>("");
  const [runtimeLabel, setRuntimeLabel] = useState("数据源未确认");
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [restoreState, setRestoreState] = useState<RestoreState>("loading");
  const [interrupted, setInterrupted] = useState(false);
  const [sessionStatus, setSessionStatus] = useState<SessionState["status"] | null>(null);
  const [pendingApprovals, setPendingApprovals] = useState<ApprovalPendingPayload[]>([]);
  const liveConnection = useRef(false);

  const [submittingApprovals, setSubmittingApprovals] = useState<Set<string>>(new Set());
  const approvalInFlight = useRef(new Set<string>());
  const listRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const lastEventAtRef = useRef(Date.now());
  const [stalled, setStalled] = useState(false);

  // 心跳与业务事件都刷新 lastEventAt；30 秒都没有才提示（设计 5.4）——
  // 这是一条连接提示，不能判定任务失败
  useEffect(() => {
    if (!busy) {
      setStalled(false);
      return;
    }
    const id = window.setInterval(
      () => setStalled(Date.now() - lastEventAtRef.current > 30000),
      5000,
    );
    return () => window.clearInterval(id);
  }, [busy]);

  // 等待时长每秒刷新；只更新显示，不写入消息内容
  const hasLiveTurn = turns.some(t => isAssistantTurn(t) && !isTerminalPhase(t.phase));
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!hasLiveTurn && !busy) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasLiveTurn, busy]);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    followRef.current = atBottom;
    setShowJump(!atBottom);
  };

  const scrollToEnd = (force = false) => {
    if (!force && !followRef.current) return;
    requestAnimationFrame(() => {
      listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
    });
  };

  const updateLastAssistant = useCallback(
    (fn: (turn: AssistantTurn) => AssistantTurn) => {
      setTurns(prev => {
        const next = [...prev];
        for (let i = next.length - 1; i >= 0; i--) {
          const turn = next[i];
          if (isAssistantTurn(turn)) {
            next[i] = fn(turn);
            return next;
          }
        }
        return next;
      });
      scrollToEnd();
    },
    [],
  );

  const consumeEvents = useCallback(async (events: AsyncIterable<SSEEvent>) => {
    // 正文增量按浏览器帧批量合并，避免每个微小片段都触发重排
    let deltaBuffer = "";
    let deltaScheduled = false;
    const flushDelta = () => {
      deltaScheduled = false;
      const text = deltaBuffer;
      deltaBuffer = "";
      if (!text) return;
      updateLastAssistant(t =>
        applyTurnEvent(t, { event: "delta", data: { text } }),
      );
    };

    for await (const ev of events) {
      lastEventAtRef.current = Date.now();
      if (ev.event !== "delta") flushDelta();
      switch (ev.event) {
        case "heartbeat":
          // 心跳只代表连接存活，不代表业务进度；不写入消息内容
          break;
        case "start":
          setStatus(`${ev.data.model}`);
          setSessionStatus("running");
          updateLastAssistant(t => applyTurnEvent(t, ev));
          break;
        case "step":
        case "reasoning_delta":
        case "tool_executing":
        case "tool_started":
        case "tool_finished":
          // 阶段与工具状态在当前回答内部展示，顶部只保留简短会话状态
          updateLastAssistant(t => applyTurnEvent(t, ev));
          break;
        case "delta":
          deltaBuffer += ev.data.text;
          if (!deltaScheduled) {
            deltaScheduled = true;
            requestAnimationFrame(flushDelta);
          }
          break;
        case "approval_pending": {
          const pending = ev.data;
          setPendingApprovals(prev => [
            ...prev.filter(item => item.pending_id !== pending.pending_id), pending,
          ]);
          setSessionStatus("waiting_approval");
          updateLastAssistant(t => applyTurnEvent(t, ev));
          break;
        }
        case "approval_resolved": {
          const resolved = ev.data;
          setPendingApprovals(prev => prev.filter(item => item.pending_id !== resolved.pending_id));
          setSessionStatus("running");
          updateLastAssistant(t => applyTurnEvent(t, ev));
          break;
        }
        case "done":
          updateLastAssistant(t => applyTurnEvent(t, ev));
          setStatus("");
          setInterrupted(false);
          setSessionStatus("completed");
          setPendingApprovals([]);
          break;
        case "error":
          updateLastAssistant(t => applyTurnEvent(t, ev));
          throw new ApiError(200, ev.data.code, ev.data.message);
      }
    }
    flushDelta();
    // 流结束但未收到 done：区分非预期 EOF，不静默变成完成；保留已收到内容
    setTurns(prev => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        const turn = next[i];
        if (isAssistantTurn(turn) && !isTerminalPhase(turn.phase)) {
          next[i] = markDisconnected(turn);
          break;
        }
      }
      return next;
    });
  }, [updateLastAssistant]);

  const applySnapshot = useCallback((snapshot: SessionState | null) => {
    if (snapshot) {
      setPendingApprovals(snapshot.pending_approvals);
      setSessionStatus(snapshot.status);
      setInterrupted(snapshot.status === "interrupted");
      setStatus(snapshot.status === "waiting_approval"
        ? "待审批：请确认下方操作"
        : snapshot.status === "interrupted" ? "任务中断，可继续恢复"
        : snapshot.status === "running" ? "任务仍在执行，正在同步状态…" : "");
      // 活跃连接正在流式更新当前轮次：快照不覆盖 turns，避免正文/工具重复
      if (!liveConnection.current) {
        setTurns(hydrateTurns(snapshot));
      }
      localStorage.removeItem(FRESH_SESSION_KEY);
      setRestoreState("ready");
    } else {
      setTurns([]);
      setPendingApprovals([]);
      setSessionStatus(null);
      setInterrupted(false);
      setStatus("");
      setRestoreState("new");
    }
  }, []);

  const refreshSession = useCallback(async (signal?: AbortSignal) => {
    const sessionId = sessionIdRef.current;
    const fresh = isFreshSession(sessionId);
    setRestoreState("loading");
    try {
      const snapshot = await loadSessionState(sessionId, signal, fresh);
      if (signal?.aborted) return;
      applySnapshot(snapshot);
    } catch (e) {
      if (!signal?.aborted) {
        setRestoreState("recovery_failed");
        setError(e instanceof Error ? e.message : String(e));
      }
      throw e;
    }
  }, [applySnapshot]);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/runtime", {signal: controller.signal}).then(async response => {
      if (!response.ok) throw new Error("runtime unavailable");
      const runtime = await response.json();
      if (controller.signal.aborted) return;
      const source = runtime.data_source === "mcp" ? "真实 ERP" :
        runtime.data_source === "demo" ? "演示数据" : "数据源未确认";
      setRuntimeLabel(`${source} · ${runtime.writes_enabled ? "写入需审批" : "只读"}`);
    }).catch(() => {});
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshSession(controller.signal).catch(() => {});
    return () => controller.abort();
  }, [refreshSession]);

  useEffect(() => {
    if (restoreState !== "ready" || sessionStatus !== "running") return;
    // 周期同步状态；活跃连接期间跳过（流本身就是事实来源），断连后自动恢复同步
    const timer = window.setInterval(() => {
      if (!liveConnection.current) {
        void refreshSession().catch(() => {});
      }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [restoreState, sessionStatus, refreshSession]);

  const hasPending = sessionStatus === "waiting_approval" || pendingApprovals.length > 0 ||
    turns.some(t => isAssistantTurn(t) &&
      Object.values(t.tools).some(tool => tool.approval && !tool.approvalResolved));
  const canSend = canSendMessage(restoreState, sessionStatus, pendingApprovals.length, busy);
  const renderedPendingIds = new Set(turns.flatMap(t => isAssistantTurn(t)
    ? Object.values(t.tools)
      .filter(tool => tool.approval && !tool.approvalResolved)
      .map(tool => tool.approval!.pending_id)
    : []));
  const unmatchedPendingApprovals =
    pendingApprovals.filter(item => !renderedPendingIds.has(item.pending_id));

  const respondApproval = useCallback(
    async (pendingId: string, approved: boolean) => {
      if (approvalInFlight.current.has(pendingId)) return;
      approvalInFlight.current.add(pendingId);
      setSubmittingApprovals(new Set(approvalInFlight.current));
      setError("");
      try {
        if (liveConnection.current) {
          await submitApproval(pendingId, approved);
        } else {
          setBusy(true);
          liveConnection.current = true;
          try {
            await consumeEvents(streamApprovalResume(sessionIdRef.current, pendingId, approved));
          } finally {
            liveConnection.current = false;
            setBusy(false);
          }
        }
      } catch (e) {
        if (isConflict(e)) {
          try { await refreshSession(); } catch { /* Recovery state keeps the input locked. */ }
        }
        setError(e instanceof Error ? e.message : "审批提交失败，请重试。");
      } finally {
        approvalInFlight.current.delete(pendingId);
        setSubmittingApprovals(new Set(approvalInFlight.current));
      }
    },
    [consumeEvents, refreshSession],
  );

  const continueInterrupted = useCallback(async () => {
    if (busy || !interrupted) return;
    setBusy(true);
    setError("");
    setStatus("正在恢复未完成任务…");
    liveConnection.current = true;
    try {
      await consumeEvents(streamInterruptedResume(sessionIdRef.current));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      liveConnection.current = false;
      setBusy(false);
    }
  }, [busy, interrupted, consumeEvents]);

  const startNewSession = useCallback(() => {
    const sessionId = newSessionId();
    localStorage.setItem(SESSION_KEY, sessionId);
    localStorage.setItem(FRESH_SESSION_KEY, sessionId);
    sessionIdRef.current = sessionId;
    setTurns([]);
    setPendingApprovals([]);
    setSessionStatus(null);
    setInterrupted(false);
    setError("");
    setStatus("");
    setInput("");
    setRestoreState("new");
  }, []);

  const send = useCallback(async () => {
    const message = input.trim();
    if (!message || !canSend || hasPending || interrupted) return;
    localStorage.removeItem(FRESH_SESSION_KEY);
    setInput("");
    setError("");
    setBusy(true);
    setSessionStatus("running");
    setTurns(prev => [
      ...prev,
      { role: "user", text: message },
      newAssistantTurn(),
    ]);
    followRef.current = true;
    scrollToEnd(true);

    liveConnection.current = true;
    try {
      await consumeEvents(streamChat(message, sessionIdRef.current));
    } catch (e) {
      if (isConflict(e)) {
        setTurns(prev => prev.slice(0, -2));
        try { await refreshSession(); } catch { /* Keep recovery failure visible and locked. */ }
      }
      setError(e instanceof Error ? e.message : String(e));
      setStatus("");
    } finally {
      liveConnection.current = false;
      setBusy(false);
    }
  }, [input, canSend, hasPending, interrupted, consumeEvents, refreshSession]);

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
        <span className="runtime-label">{runtimeLabel}</span>
        <button className="new-session" onClick={startNewSession}
          disabled={busy || hasPending || interrupted}>新会话</button>
        <span className="status" role="status">
          {status || (restoreState === "loading" ? "加载会话…" : busy ? "生成中…" : "")}
        </span>
      </header>

      <div className="chat" ref={listRef} onScroll={onScroll}>
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
            <AssistantMessage
              key={i}
              turn={turn}
              now={now}
              live={busy}
              stalled={stalled}
              submittingApprovals={submittingApprovals}
              onRespond={(pendingId, approved) => void respondApproval(pendingId, approved)}
            />
          ),
        )}
        {unmatchedPendingApprovals.map(pending => (
          <div className="approval" key={pending.pending_id}>
            <span className="approval-label">
              待审批 · {pending.tool} · 风险等级 {pending.risk}
            </span>
            <pre className="args">{JSON.stringify(pending.arguments)}</pre>
            <button className="approve" disabled={submittingApprovals.has(pending.pending_id)}
              onClick={() => void respondApproval(pending.pending_id, true)}>批准</button>
            <button className="deny" disabled={submittingApprovals.has(pending.pending_id)}
              onClick={() => void respondApproval(pending.pending_id, false)}>拒绝</button>
          </div>
        ))}
        {sessionStatus === "waiting_approval" && pendingApprovals.length === 0 && (
          <div className="error" role="status">任务仍在等待审批；审批详情暂不可用，请刷新会话状态。</div>
        )}
        {restoreState === "recovery_failed" && (
          <div className="error" role="alert">
            会话恢复失败，当前输入已锁定。请重试恢复，或显式开始新会话。
          </div>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {showJump && (busy || hasLiveTurn) && (
          <button className="jump-latest" onClick={() => {
            followRef.current = true;
            scrollToEnd(true);
            setShowJump(false);
          }}>
            回到最新 ↓
          </button>
        )}
      </div>

      <footer>
        {restoreState === "recovery_failed" && (
          <>
            <button onClick={() => void refreshSession().catch(() => {})}>重试恢复</button>
            <button onClick={startNewSession}>开始新会话</button>
          </>
        )}
        {interrupted && (
          <button onClick={() => void continueInterrupted()} disabled={busy || restoreState === "loading"}>
            继续恢复
          </button>
        )}
        <textarea
          aria-label="消息"
          value={input}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={2}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={!canSend || hasPending || interrupted}
        />
        <button onClick={() => void send()} disabled={!canSend || hasPending || interrupted || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  );
}
