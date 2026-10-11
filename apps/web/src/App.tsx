import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import {
  type ApprovalPendingPayload,
  type SSEEvent,
  type SessionState,
  ApiError,
  cancelSession,
  loadSessionState,
  streamInterruptedResume,
  streamRunEvents,
  streamApprovalResume,
  streamChat,
  submitApproval,
} from "./protocol";

import {
  type AssistantTurn,
  type RestoreState,
  type Turn,
  applyTurnEvent,
  acceptEventCursor,
  canSendMessage,
  hydrateTurns,
  isAssistantTurn,
  isTerminalPhase,
  markDisconnected,
  newAssistantTurn,
} from "./session";
import { AssistantMessage } from "./components/AssistantMessage";
import { approvalDraftIssue, createOrderDraft, getOrderDraft, listOrderDrafts, parseOrderDraft, reviseOrderDraft, type OrderDraft } from './drafts';
import { DraftSummary, OrderDraftPanel } from './components/OrderDraftPanel';
import { approvalOperationIssue, getOperation, requiresBusinessSummary, parseOperation, retainOperationSnapshot, type BusinessOperation } from './operations';
import { BusinessApprovalSummary } from './components/BusinessApprovalSummary';
import { SessionRequests } from './history';
import { SessionHistory } from './components/SessionHistory';

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
  const sessionRequests = useRef(new SessionRequests(sessionIdRef.current));
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyButton = useRef<HTMLButtonElement>(null);
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
  const [pendingReconciliation, setPendingReconciliation] = useState<SessionState["pending_reconciliation"]>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const lastSeqByRun = useRef(new Map<string, number>());
  const liveConnection = useRef(false);

  const [submittingApprovals, setSubmittingApprovals] = useState<Set<string>>(new Set());
  const approvalInFlight = useRef(new Set<string>());
  const listRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const lastEventAtRef = useRef(Date.now());
  const [stalled, setStalled] = useState(false);
  const [draftsEnabled, setDraftsEnabled] = useState(false);
  const [writesEnabled, setWritesEnabled] = useState(false);
  const draftsEnabledRef = useRef(false);
  const [drafts, setDrafts] = useState<OrderDraft[]>([]);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [draftsError, setDraftsError] = useState('');
  const [approvalDrafts, setApprovalDrafts] = useState<Record<string, OrderDraft>>({});
  const [businessEnabled, setBusinessEnabled] = useState(false);
  const [operationRefresh, setOperationRefresh] = useState(0);
  const [approvalOperations, setApprovalOperations] = useState<Record<string,{operation?:BusinessOperation;verified:boolean;loading:boolean;error?:string}>>({});
  const operationSnapshots = useRef(new Map<string,BusinessOperation>());
  const draftRequest = useRef(0);
  const refreshDrafts = useCallback(async () => {
    if (!draftsEnabledRef.current) return;
    const sessionId = sessionIdRef.current;
    const ticket = sessionRequests.current.capture();
    const request = ++draftRequest.current;
    setDraftsLoading(true);
    try {
      const items = await listOrderDrafts(sessionId);
      if (!sessionRequests.current.isCurrent(ticket) || request !== draftRequest.current) return;
      setDrafts(items);setDraftsError('');
    } catch (error) {
      if (sessionRequests.current.isCurrent(ticket) && request === draftRequest.current) {setDrafts([]);setDraftsError(error instanceof Error ? error.message : String(error));}
    } finally {if (sessionRequests.current.isCurrent(ticket) && request === draftRequest.current) setDraftsLoading(false);}
  }, []);

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
    if (!hasLiveTurn && !busy && pendingApprovals.length === 0) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasLiveTurn, busy, pendingApprovals.length]);

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

  const consumeEvents = useCallback(async (events: AsyncIterable<SSEEvent>, isCurrent: () => boolean = () => true) => {
    const ticket = sessionRequests.current.capture();
    const valid = () => sessionRequests.current.isCurrent(ticket) && isCurrent();
    // 正文增量按浏览器帧批量合并，避免每个微小片段都触发重排
    let deltaBuffer = "";
    let deltaScheduled = false;
    const flushDelta = () => {
      deltaScheduled = false;
      const text = deltaBuffer;
      deltaBuffer = "";
      if (!text || !valid()) return;
      updateLastAssistant(t =>
        applyTurnEvent(t, { event: "delta", data: { text } }),
      );
    };

    for await (const ev of events) {
      if (!valid()) return;
      if (!acceptEventCursor(ev, lastSeqByRun.current)) continue;
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
          if (ev.event === 'tool_finished') {void refreshDrafts();setOperationRefresh(value=>value+1);}
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
          void refreshDrafts();
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
        case "reconciliation_pending":
          setPendingReconciliation(ev.data);
          setSessionStatus("reconciliation_required");
          setInterrupted(true);
          updateLastAssistant(t => applyTurnEvent(t, ev));
          break;
        case "done":
          void refreshDrafts();
          updateLastAssistant(t => applyTurnEvent(t, ev));
          setStatus("");
          setInterrupted(false);
          setSessionStatus("completed");
          setPendingApprovals([]);
          setPendingReconciliation(null);
          break;
        case "error":
          updateLastAssistant(t => applyTurnEvent(t, ev));
          throw new ApiError(200, ev.data.code, ev.data.message);
      }
    }
    flushDelta();
    if (!valid()) return;
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
  }, [updateLastAssistant, refreshDrafts]);

  const applySnapshot = useCallback((snapshot: SessionState | null) => {
    void refreshDrafts();
    if (snapshot) {
      setPendingApprovals(snapshot.pending_approvals);
      setPendingReconciliation(snapshot.pending_reconciliation ?? null);
      setSessionStatus(snapshot.status);
      setInterrupted(
        snapshot.status === "interrupted" || snapshot.status === "reconciliation_required",
      );
      setActiveRunId(snapshot.run_id ?? null);
      if (snapshot.run_id) lastSeqByRun.current.set(snapshot.run_id, snapshot.last_seq ?? 0);
      setStatus(snapshot.status === "reconciliation_required"
        ? "写入结果待核对，可再次安全查询"
        : snapshot.status === "waiting_approval"
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
      setPendingReconciliation(null);
      setActiveRunId(null);
      setSessionStatus(null);
      setInterrupted(false);
      setStatus("");
      setRestoreState("new");
    }
  }, [refreshDrafts]);

  const refreshSession = useCallback(async (signal?: AbortSignal) => {
    const sessionId = sessionIdRef.current;
    const ticket = sessionRequests.current.refresh();
    const fresh = isFreshSession(sessionId);
    setRestoreState("loading");
    try {
      const snapshot = await loadSessionState(sessionId, signal, fresh);
      if (signal?.aborted || !sessionRequests.current.isCurrent(ticket)) return;
      if (snapshot && snapshot.session_id !== sessionId) throw new Error('会话状态与所选会话不匹配');
      setError('');
      applySnapshot(snapshot);
    } catch (e) {
      if (signal?.aborted || !sessionRequests.current.isCurrent(ticket)) return;
      {
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
      setDraftsEnabled(runtime.drafts_enabled === true);
      setWritesEnabled(runtime.writes_enabled === true);
      setBusinessEnabled(runtime.business_approvals_enabled === true);
      draftsEnabledRef.current = runtime.drafts_enabled === true;
    }).catch(() => {});
    return () => controller.abort();
  }, []);

  useEffect(() => { if (draftsEnabled) void refreshDrafts(); }, [draftsEnabled, refreshDrafts]);
  useEffect(() => {
    const sessionId=sessionIdRef.current;
    const ticket=sessionRequests.current.capture();
    const controller=new AbortController();
    for(const pending of pendingApprovals) {
      if(!requiresBusinessSummary(pending,businessEnabled))continue;
      const key=pending.pending_id;
      let snapshot=operationSnapshots.current.get(key);
      let snapshotError='';
      if(!snapshot&&pending.operation!==undefined) {
        try {snapshot=parseOperation(pending.operation);operationSnapshots.current.set(key,snapshot);}
        catch(error){snapshotError=error instanceof Error?error.message:String(error);}
      }
      setApprovalOperations(prev=>({...prev,[key]:{operation:snapshot,verified:false,loading:true,error:snapshotError}}));
      const id=pending.arguments.operation_id;
      if(typeof id!=='string'||!id||snapshotError) {
        setApprovalOperations(prev=>({...prev,[key]:{operation:snapshot,verified:false,loading:false,error:snapshotError||'审批引用缺失，请拒绝后重新准备。'}}));
        continue;
      }
      void getOperation(sessionId,id,controller.signal).then(current=>{
        if(controller.signal.aborted||!sessionRequests.current.isCurrent(ticket))return;
        const operation=retainOperationSnapshot(snapshot,current);
        operationSnapshots.current.set(key,operation);
        setApprovalOperations(prev=>({...prev,[key]:{operation,verified:true,loading:false}}));
      }).catch(error=>{
        if(controller.signal.aborted||!sessionRequests.current.isCurrent(ticket))return;
        setApprovalOperations(prev=>({...prev,[key]:{operation:snapshot,verified:false,loading:false,error:error instanceof Error?error.message:String(error)}}));
      });
    }
    return ()=>controller.abort();
  },[pendingApprovals,operationRefresh,businessEnabled]);
  useEffect(() => {
    const sessionId = sessionIdRef.current;
    const ticket = sessionRequests.current.capture();
    const controller = new AbortController();
    for (const pending of pendingApprovals) {
      if (pending.tool !== 'submit_order_draft') continue;
      const id = pending.arguments.draft_id, revision = pending.arguments.revision;
      if (typeof id !== 'string' || typeof revision !== 'number' || !Number.isSafeInteger(revision)) continue;
      if (pending.draft) {
        try { const draft = parseOrderDraft(pending.draft); setApprovalDrafts(prev=>({...prev,[pending.pending_id]:draft})); } catch { /* Fail closed; retrieve the approved version. */ }
      }
      void getOrderDraft(sessionId,id,revision,controller.signal).then(draft=>{
        if (!controller.signal.aborted && sessionRequests.current.isCurrent(ticket)) setApprovalDrafts(prev=>({...prev,[pending.pending_id]:draft}));
      }).catch(()=>{});
    }
    return ()=>controller.abort();
  }, [pendingApprovals]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshSession(controller.signal).catch(() => {});
    return () => controller.abort();
  }, [refreshSession]);

  useEffect(() => {
    if (restoreState !== "ready" || !activeRunId || sessionStatus === "completed") return;
    const controller = new AbortController();
    const ticket = sessionRequests.current.capture();
    void consumeEvents(streamRunEvents(
      activeRunId, lastSeqByRun.current.get(activeRunId) ?? 0, controller.signal,
    ), () => !controller.signal.aborted && sessionRequests.current.isCurrent(ticket)).catch(error => {
      if (!controller.signal.aborted && sessionRequests.current.isCurrent(ticket)) {
        setError(error instanceof Error ? error.message : String(error));
      }
    });
    return () => controller.abort();
  }, [restoreState, activeRunId, sessionStatus, consumeEvents]);

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

  const hasPending = sessionStatus === "waiting_approval" ||
    sessionStatus === "reconciliation_required" || pendingApprovals.length > 0 ||
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
      const ticket = sessionRequests.current.capture();
      const pending = pendingApprovals.find(item=>item.pending_id===pendingId);
      if(approved&&!pending)return;
      const operationState=approvalOperations[pendingId];
      if(approved&&pending&&requiresBusinessSummary(pending,businessEnabled)&&(!businessEnabled||operationState?.loading||approvalOperationIssue(pending,operationState?.operation,operationState?.verified===true)))return;
      if (approved && pending?.tool==='submit_order_draft' && (draftsLoading || approvalDraftIssue(pending,approvalDrafts[pendingId],drafts))) return;
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
        if (!sessionRequests.current.isCurrent(ticket)) return;
        if (isConflict(e)) {
          try { await refreshSession(); } catch { /* Recovery state keeps the input locked. */ }
        }
        setError(e instanceof Error ? e.message : "审批提交失败，请重试。");
      } finally {
        if (!sessionRequests.current.isCurrent(ticket)) return;
        approvalInFlight.current.delete(pendingId);
        setSubmittingApprovals(new Set(approvalInFlight.current));
      }
    },
    [consumeEvents, refreshSession, pendingApprovals, approvalDrafts, drafts, draftsLoading, approvalOperations, businessEnabled],
  );

  const continueInterrupted = useCallback(async () => {
    if (busy || !interrupted) return;
    const ticket = sessionRequests.current.capture();
    setBusy(true);
    setError("");
    setStatus("正在恢复未完成任务…");
    liveConnection.current = true;
    try {
      await consumeEvents(streamInterruptedResume(sessionIdRef.current));
    } catch (e) {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      liveConnection.current = false;
      setBusy(false);
    }
  }, [busy, interrupted, consumeEvents]);

  const cancelPending = useCallback(async () => {
    const ticket = sessionRequests.current.capture();
    setBusy(true);
    setError("");
    try {
      const snapshot = await cancelSession(sessionIdRef.current);
      if (sessionRequests.current.isCurrent(ticket)) applySnapshot(snapshot);
    } catch (e) {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      setError(e instanceof Error ? e.message : "取消失败");
    } finally {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      setBusy(false);
    }
  }, [applySnapshot]);

  const startNewSession = useCallback(() => {
    if (busy || liveConnection.current || sessionStatus === 'running') return;
    const sessionId = newSessionId();
    localStorage.setItem(SESSION_KEY, sessionId);
    localStorage.setItem(FRESH_SESSION_KEY, sessionId);
    sessionIdRef.current = sessionId;
    sessionRequests.current.switchTo(sessionId);
    lastSeqByRun.current.clear();
    approvalInFlight.current.clear();setSubmittingApprovals(new Set());
    setShowJump(false);followRef.current=true;
    draftRequest.current++;
    operationSnapshots.current.clear();setApprovalOperations({});
    setDrafts([]);setApprovalDrafts({});setDraftsError('');setDraftsLoading(false);
    if (draftsEnabled) void refreshDrafts();
    setTurns([]);
    setPendingApprovals([]);
    setPendingReconciliation(null);
    setActiveRunId(null);
    setSessionStatus(null);
    setInterrupted(false);
    setError("");
    setStatus("");
    setInput("");
    setRestoreState("new");
  }, [draftsEnabled, refreshDrafts, busy, sessionStatus]);

  const selectHistorySession = useCallback((sessionId: string) => {
    if (busy || liveConnection.current || sessionStatus === 'running') return;
    sessionRequests.current.switchTo(sessionId);
    sessionIdRef.current = sessionId;
    localStorage.setItem(SESSION_KEY, sessionId);
    localStorage.removeItem(FRESH_SESSION_KEY);
    draftRequest.current++;
    operationSnapshots.current.clear();lastSeqByRun.current.clear();
    approvalInFlight.current.clear();setSubmittingApprovals(new Set());
    setTurns([]);setInput('');setStatus('');setError('');
    setPendingApprovals([]);setPendingReconciliation(null);setActiveRunId(null);
    setSessionStatus(null);setInterrupted(false);
    setDrafts([]);setApprovalDrafts({});setApprovalOperations({});
    setDraftsError('');setDraftsLoading(false);setShowJump(false);followRef.current=true;
    setHistoryOpen(false);historyButton.current?.focus();
    void refreshSession().catch(() => {});
  }, [busy, sessionStatus, refreshSession]);

  const send = useCallback(async (requestedMessage?: string) => {
    const message = (requestedMessage ?? input).trim();
    if (!message || !canSend || hasPending || interrupted) return;
    const ticket = sessionRequests.current.capture();
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
      if (!sessionRequests.current.isCurrent(ticket)) return;
      if (isConflict(e)) {
        setTurns(prev => prev.slice(0, -2));
        try { await refreshSession(); } catch { /* Keep recovery failure visible and locked. */ }
      }
      setError(e instanceof Error ? e.message : String(e));
      setStatus("");
    } finally {
      if (!sessionRequests.current.isCurrent(ticket)) return;
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

  const renderDraftApproval = (pending: ApprovalPendingPayload) => {
    const draft = approvalDrafts[pending.pending_id];
    const issue = approvalDraftIssue(pending,draft,drafts,now);
    const expired = pending.expires_at != null && Date.parse(pending.expires_at)<=now;
    return <div className="approval draft-approval"><span className="approval-label">待审批 · 提交订单 · 原版本 {String(pending.arguments.revision)}</span>
      {draft && <DraftSummary draft={draft}/>}{(issue || expired) && <p role="status">{expired?'审批已过期，请拒绝后重新提交。':issue}</p>}
      <button className="approve" disabled={!!issue||expired||draftsLoading||submittingApprovals.has(pending.pending_id)} onClick={()=>void respondApproval(pending.pending_id,true)}>批准</button>
      <button className="deny" disabled={submittingApprovals.has(pending.pending_id)} onClick={()=>void respondApproval(pending.pending_id,false)}>拒绝</button>
    </div>;
  };

  const renderBusinessApproval = (pending:ApprovalPendingPayload) => {
    const state=approvalOperations[pending.pending_id];
    return <BusinessApprovalSummary pending={pending} operation={state?.operation} verified={state?.verified===true} enabled={businessEnabled} loading={state?.loading===true} error={state?.error} now={now} submitting={submittingApprovals.has(pending.pending_id)} onRespond={(id,approved)=>void respondApproval(id,approved)} onRefresh={()=>{
      setApprovalOperations(prev=>({...prev,[pending.pending_id]:{...prev[pending.pending_id],verified:false,loading:true}}));
      setOperationRefresh(value=>value+1);
    }}/>;
  };

  return (
    <div className="app">
      <header>
        <h1>Erpilot 掌柜助手</h1>
        <span className="runtime-label">{runtimeLabel}</span>
        <button ref={historyButton} className="new-session" aria-expanded={historyOpen} aria-controls="session-history-panel"
          onClick={() => setHistoryOpen(value => !value)}>历史会话</button>
        <button className="new-session" onClick={startNewSession}
          disabled={busy || hasPending || interrupted || sessionStatus === 'running'}>新会话</button>
        <span className="status" role="status">
          {status || (restoreState === "loading" ? "加载会话…" : busy ? "生成中…" : "")}
        </span>
      </header>

      {historyOpen && <div id="session-history-panel"><SessionHistory currentId={sessionIdRef.current}
        locked={busy || sessionStatus === 'running'} onSelect={selectHistorySession}
        onClose={() => {setHistoryOpen(false);historyButton.current?.focus();}} /></div>}

      <div className="chat" ref={listRef} onScroll={onScroll}>
        {draftsEnabled && <OrderDraftPanel key={sessionIdRef.current} drafts={drafts} loading={draftsLoading} error={draftsError} busy={busy && !hasPending} canSubmit={writesEnabled&&canSend&&!hasPending&&!interrupted} writesEnabled={writesEnabled} hasPending={hasPending}
          onRefresh={()=>void refreshDrafts()} onSave={async (draft,input)=>{
            const sessionId=sessionIdRef.current;
            const ticket=sessionRequests.current.capture();
            setDraftsLoading(true);
            try {
              const saved=draft?await reviseOrderDraft(sessionId,draft.draft_id,draft.revision,input):await createOrderDraft(sessionId,input);
              if(!sessionRequests.current.isCurrent(ticket))return;
              setDrafts(prev=>[saved,...prev.filter(item=>item.draft_id!==saved.draft_id)]);
              return saved;
            } finally {if(sessionRequests.current.isCurrent(ticket)) await refreshDrafts();}
          }} onSubmit={draft=>{
            if (Date.parse(draft.expires_at)<=Date.now()) {void refreshDrafts();return;}
            void send(`请调用 submit_order_draft 提交草稿 ${draft.draft_id} 的版本 ${draft.revision}，并申请审批。`);
          }} />}
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
              renderDraftApproval={renderDraftApproval}
              renderBusinessApproval={renderBusinessApproval}
              businessApprovalsEnabled={businessEnabled}
            />
          ),
        )}
        {unmatchedPendingApprovals.map(pending => (
          requiresBusinessSummary(pending,businessEnabled) ? <div key={pending.pending_id}>{renderBusinessApproval(pending)}</div> :
          pending.tool === 'submit_order_draft' ? <div key={pending.pending_id}>{renderDraftApproval(pending)}</div> :
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
            {sessionStatus === "reconciliation_required" ? "再次核对" : "继续恢复"}
          </button>
        )}
        {(pendingApprovals.length > 0 || pendingReconciliation) && (
          <button onClick={() => void cancelPending()} disabled={busy}>
            取消待处理操作
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
