import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import { Alert, Button, Empty, Input, Tabs, Tag, type GetProps } from "antd";
import { Bubble, Prompts, Sender } from "@ant-design/x";
import { ArrowDownOutlined, ArrowUpOutlined, CheckCircleOutlined, InboxOutlined, FileTextOutlined, ShoppingOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { WorkspaceShell } from "./components/WorkspaceShell";
import { BusinessResultCard, isUsableDisplay } from "./components/BusinessResultCard";
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

function MessageInput(props: GetProps<typeof Input.TextArea>) {
  return <Input.TextArea {...props} aria-label="消息" />;
}
const SENDER_COMPONENTS = { input: MessageInput };

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
  const [businessOpen, setBusinessOpen] = useState(() => window.matchMedia("(min-width: 1200px)").matches);
  const [businessTab, setBusinessTab] = useState("drafts");
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
  useEffect(() => {
    if (pendingApprovals.length > 0 || pendingReconciliation) setBusinessTab("approvals");
  }, [pendingApprovals.length, pendingReconciliation]);
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
        ? "待审批：请在业务面板确认操作"
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
    setHistoryOpen(false);
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

    let streamFailure: string | null = null;
    liveConnection.current = true;
    try {
      await consumeEvents(streamChat(message, sessionIdRef.current));
    } catch (e) {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      if (isConflict(e)) {
        setTurns(prev => prev.slice(0, -2));
        try { await refreshSession(); } catch { /* Keep recovery failure visible and locked. */ }
      }
      streamFailure = e instanceof Error ? e.message : String(e);
      setError(streamFailure);
      setStatus("");
    } finally {
      if (!sessionRequests.current.isCurrent(ticket)) return;
      liveConnection.current = false;
      if (streamFailure !== null) {
        // Verify durable state after a failed stream; never retry business writes.
        try {
          await refreshSession();
          if (sessionRequests.current.isCurrent(ticket)) setError(streamFailure);
        } catch { /* Recovery failure retains the input lock and retry controls. */ }
      }
      setBusy(false);
    }
  }, [input, canSend, hasPending, interrupted, consumeEvents, refreshSession]);

  const renderDraftApproval = (pending: ApprovalPendingPayload) => {
    const draft = approvalDrafts[pending.pending_id];
    const issue = approvalDraftIssue(pending,draft,drafts,now);
    const expired = pending.expires_at != null && Date.parse(pending.expires_at)<=now;
    return <div className="approval draft-approval"><span className="approval-label">待审批 · 提交订单 · 原版本 {String(pending.arguments.revision)}</span>
      {draft && <DraftSummary draft={draft}/>}{(issue || expired) && <p role="status">{expired?'审批已过期，请拒绝后重新提交。':issue}</p>}
      <Button type="primary" className="approve" disabled={!!issue||expired||draftsLoading||submittingApprovals.has(pending.pending_id)} onClick={()=>void respondApproval(pending.pending_id,true)}>批准</Button>
      <Button className="deny" disabled={submittingApprovals.has(pending.pending_id)} onClick={()=>void respondApproval(pending.pending_id,false)}>拒绝</Button>
    </div>;
  };

  const renderBusinessApproval = (pending:ApprovalPendingPayload) => {
    const state=approvalOperations[pending.pending_id];
    return <BusinessApprovalSummary pending={pending} operation={state?.operation} verified={state?.verified===true} enabled={businessEnabled} loading={state?.loading===true} error={state?.error} now={now} submitting={submittingApprovals.has(pending.pending_id)} onRespond={(id,approved)=>void respondApproval(id,approved)} onRefresh={()=>{
      setApprovalOperations(prev=>({...prev,[pending.pending_id]:{...prev[pending.pending_id],verified:false,loading:true}}));
      setOperationRefresh(value=>value+1);
    }}/>;
  };

  const results = turns.flatMap(turn => isAssistantTurn(turn)
    ? Object.values(turn.tools).filter(tool => isUsableDisplay(tool.display)).map(tool => ({ key: tool.id, display: tool.display! }))
    : []);
  const sessionLocked = busy || hasPending || interrupted || sessionStatus === 'running';
  const inputLocked = !canSend || hasPending || interrupted;
  const inspectApprovals = () => { setBusinessTab('approvals'); setBusinessOpen(true); };
  const orderPanel = draftsEnabled ? <OrderDraftPanel key={sessionIdRef.current} drafts={drafts} loading={draftsLoading} error={draftsError} busy={busy && !hasPending} canSubmit={writesEnabled&&canSend&&!hasPending&&!interrupted} writesEnabled={writesEnabled} hasPending={hasPending}
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
          }} /> :
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前环境尚未开放订单草稿" />;
  const approvalPanel = <div className="approval-list">
    {pendingApprovals.length === 0 ? <Empty image={<CheckCircleOutlined />} description="暂无待处理审批" /> :
      pendingApprovals.map(pending => <div key={pending.pending_id}>
        {requiresBusinessSummary(pending, businessEnabled) ? renderBusinessApproval(pending) :
          pending.tool === 'submit_order_draft' ? renderDraftApproval(pending) :
          <section className="approval" aria-label="操作审批">
            <h3>待审批 · {pending.tool}</h3><Tag>风险等级 {pending.risk}</Tag>
            <pre className="args">{JSON.stringify(pending.arguments, null, 2)}</pre>
            {pending.expires_at && Date.parse(pending.expires_at) <= now && <Alert type="warning" title="审批已过期，请拒绝后重新准备。" />}
            <div className="approval-actions"><Button type="primary" className="approve"
              disabled={submittingApprovals.has(pending.pending_id) || (pending.expires_at != null && Date.parse(pending.expires_at) <= now)}
              onClick={() => void respondApproval(pending.pending_id, true)}>批准</Button>
              <Button className="deny" disabled={submittingApprovals.has(pending.pending_id)}
                onClick={() => void respondApproval(pending.pending_id, false)}>拒绝</Button></div>
          </section>}
      </div>)}
    {pendingReconciliation && <Alert type="warning" showIcon title="操作结果待核对" description="请再次核对后继续处理，避免重复写入。" />}
  </div>;
  const business = <Tabs className="business-tabs" activeKey={businessTab} onChange={setBusinessTab} items={[
    { key: 'drafts', label: '订单草稿', children: orderPanel },
    { key: 'approvals', label: <span>审批 {pendingApprovals.length > 0 && <span className="tab-count">{pendingApprovals.length}</span>}</span>, children: approvalPanel },
    { key: 'results', label: '业务结果', children: <div className="business-results">
      {results.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="查询或操作完成后，结果会显示在这里" /> :
        results.map((result, index) => <BusinessResultCard key={result.key + '-' + index} display={result.display} />)}
    </div> },
  ]} />;
  const composer = <>
    <div className="recovery-actions">
      {restoreState === 'recovery_failed' && <><Button onClick={() => void refreshSession().catch(() => {})}>重试恢复</Button><Button onClick={startNewSession}>开始新会话</Button></>}
      {interrupted && <Button onClick={() => void continueInterrupted()} disabled={busy || restoreState === 'loading'}>
        {sessionStatus === 'reconciliation_required' ? '再次核对' : '继续恢复'}</Button>}
      {(pendingApprovals.length > 0 || pendingReconciliation) && <Button onClick={() => void cancelPending()} disabled={busy}>取消待处理操作</Button>}
    </div>
    <Sender value={input} onChange={setInput} onSubmit={message => void send(message)} disabled={inputLocked}
      autoSize={{ minRows: 2, maxRows: 6 }} placeholder={hasPending ? '请先处理待审批操作' : interrupted ? '请先恢复或核对当前任务' : '描述你要处理的业务…'}
      components={SENDER_COMPONENTS}
      suffix={(_, { components: { SendButton } }) => <SendButton type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="发送"
        disabled={inputLocked || !input.trim()} />}
      footer={<div className="composer-footer"><span>{hasPending ? '操作等待你的确认' : busy ? '正在处理，请稍候' : 'Enter 发送 · Shift + Enter 换行'}</span>
        <span><CheckCircleOutlined /> 写入操作需审批</span></div>} />
  </>;
  return <WorkspaceShell sidebar={<SessionHistory currentId={sessionIdRef.current} locked={busy || sessionStatus === 'running'} onSelect={selectHistorySession} />}
    composer={composer} business={business} runtimeLabel={runtimeLabel}
    status={status || (restoreState === 'loading' ? '正在恢复会话…' : busy ? '正在处理…' : '')}
    historyOpen={historyOpen} businessOpen={businessOpen} sessionLocked={sessionLocked}
    onHistoryOpen={setHistoryOpen} onBusinessOpen={setBusinessOpen} onNewSession={startNewSession}>
    <div className="chat" ref={listRef} onScroll={onScroll}>
      <div className="conversation-content">
        {turns.length === 0 && restoreState !== 'recovery_failed' && <div className="welcome">
          <div className="welcome-symbol"><ThunderboltOutlined /></div>
          <h2>今天，想处理什么业务？</h2><p>查询订单、核对库存、整理报价。<br />把需求告诉我，我们一步步完成。</p>
          <Prompts className="welcome-prompts" items={[
            { key: 'order', icon: <ShoppingOutlined />, label: '查询订单', description: '查看商品与订单明细' },
            { key: 'stock', icon: <InboxOutlined />, label: '核对库存', description: '确认可售数量' },
            { key: 'quote', icon: <FileTextOutlined />, label: '整理报价', description: '核对价格与商品数量' },
          ]} onItemClick={({ data }) => setInput(data.key === 'order' ? '帮我查询订单：' : data.key === 'stock' ? '帮我查询商品库存，SKU：' : '帮我整理商品报价，商品与数量：')} />
        </div>}
        {turns.map((turn, i) => turn.role === 'user' ? <Bubble key={i} className="user-message" placement="end" content={turn.text} /> :
          <AssistantMessage key={i} turn={turn} now={now} live={busy} stalled={stalled}
            submittingApprovals={submittingApprovals} onRespond={(id, approved) => void respondApproval(id, approved)}
            renderDraftApproval={renderDraftApproval} renderBusinessApproval={renderBusinessApproval}
            businessApprovalsEnabled={businessEnabled} onInspectApproval={inspectApprovals} />)}
        {hasPending && <Alert className="pending-notice" type="warning" showIcon title={pendingReconciliation ? '有操作结果需要核对' : '有操作等待你确认'}
          description="请查看业务面板中的明细，再继续处理。" action={<Button size="small" onClick={inspectApprovals}>查看审批</Button>} />}
        {sessionStatus === 'waiting_approval' && pendingApprovals.length === 0 && <Alert type="warning" showIcon
          title="审批详情暂不可用" description="任务仍在等待审批，请刷新会话状态。" action={<Button onClick={() => void refreshSession().catch(() => {})}>刷新状态</Button>} />}
        {restoreState === 'recovery_failed' && <Alert type="error" showIcon title="会话恢复失败" description="当前输入已锁定。请重试恢复，或开始新会话。" />}
        {error && <Alert type="error" showIcon title={error} />}
      </div>
    </div>
    {showJump && (busy || hasLiveTurn) && <Button className="jump-latest" icon={<ArrowDownOutlined />} onClick={() => {
      followRef.current = true; scrollToEnd(true); setShowJump(false);
    }}>回到最新</Button>}
  </WorkspaceShell>;
}
