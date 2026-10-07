/**
 * 会话展示模型：一轮用户请求 + 其后的助手回答构成一个展示轮次（turn）。
 *
 * 内容按收到事件的真实顺序存进有序 blocks；工具实体按 callId 单独保存，
 * 块只引用实体，完成状态独立更新、不打乱展示顺序。阶段状态是轮次字段，
 * 计时刷新不写入 blocks。reasoning 块为阶段二预留，当前不产生。
 */

import type {
  ApprovalPendingPayload,
  ApprovalResolvedPayload,
  BusinessDisplay,
  DonePayload,
  PresentationTurn,
  SessionState,
  SSEEvent,
  ToolFinishedPayload,
} from "./protocol";

export type ToolStatus =
  | "prepared"
  | "waiting_approval"
  | "running"
  | "succeeded"
  | "failed"
  | "denied"
  | "unknown";

export interface ToolEntity {
  id: string;
  name: string;
  arguments: string;
  status: ToolStatus;
  finished: ToolFinishedPayload | null;
  /** 业务展示卡片数据（设计 5.2）；契约不符时前端回退通用详情 */
  display: BusinessDisplay | null;
  approval: ApprovalPendingPayload | null;
  approvalResolved: ApprovalResolvedPayload | null;
}

export type MessageBlock =
  | { id: string; type: "text"; step: number; text: string }
  | { id: string; type: "tool"; callId: string }
  | { id: string; type: "reasoning"; step: number; text: string };

export type TurnPhase =
  | "connecting"
  | "started"
  | "analyzing"
  | "generating"
  | "awaiting_approval"
  | "approved"
  | "completed"
  | "max_steps"
  | "failed"
  | "disconnected";

export interface AssistantTurn {
  role: "assistant";
  blocks: MessageBlock[];
  tools: Record<string, ToolEntity>;
  phase: TurnPhase;
  step: number;
  /** 本地进入当前阶段的时刻，仅用于显示等待时长，不参与业务。 */
  phaseAt: number;
  done: DonePayload | null;
  error: string | null;
}

export interface UserTurn {
  role: "user";
  text: string;
}

export type Turn = UserTurn | AssistantTurn;

export type RestoreState = "loading" | "new" | "ready" | "recovery_failed";

export function isAssistantTurn(turn: Turn): turn is AssistantTurn {
  return turn.role === "assistant";
}

const TERMINAL_PHASES: ReadonlySet<TurnPhase> = new Set([
  "completed", "max_steps", "failed", "disconnected",
]);

export function isTerminalPhase(phase: TurnPhase): boolean {
  return TERMINAL_PHASES.has(phase);
}

let blockSeq = 0;
// 命名空间隔离：模块重载（HMR/热更新）后计数器归零，避免与旧树中的块 id 撞车
const BLOCK_NS = Math.random().toString(36).slice(2, 8);
function nextBlockId(): string {
  blockSeq += 1;
  return `b${BLOCK_NS}-${blockSeq}`;
}

export function newAssistantTurn(now = Date.now()): AssistantTurn {
  return {
    role: "assistant",
    blocks: [],
    tools: {},
    phase: "connecting",
    step: 0,
    phaseAt: now,
    done: null,
    error: null,
  };
}

function newTextBlock(step: number): Extract<MessageBlock, { type: "text" }> {
  return { id: nextBlockId(), type: "text", step, text: "" };
}

function newToolEntity(
  id: string, name: string, args: string,
): ToolEntity {
  return {
    id, name, arguments: args, status: "prepared",
    finished: null, display: null, approval: null, approvalResolved: null,
  };
}

/**
 * 把一个 SSE 事件应用到助手轮次：纯函数，返回新对象。
 * delta 在相邻文本块间合并；工具边界后另起新文本块；
 * 拒绝过的工具不被 tool_finished 的 ok=true 翻转为成功。
 */
export function applyTurnEvent(
  turn: AssistantTurn, ev: SSEEvent, now = Date.now(),
): AssistantTurn {
  const withPhase = (phase: TurnPhase, patch: Partial<AssistantTurn> = {}): AssistantTurn =>
    phase === turn.phase
      ? { ...turn, ...patch }
      : { ...turn, ...patch, phase, phaseAt: now };

  switch (ev.event) {
    case "start":
      return withPhase("started");
    case "step":
      return withPhase("analyzing", { step: ev.data.step });
    case "delta": {
      const blocks = [...turn.blocks];
      const last = blocks[blocks.length - 1];
      if (last && last.type === "text" && last.step === turn.step) {
        blocks[blocks.length - 1] = { ...last, text: last.text + ev.data.text };
      } else {
        blocks.push({ ...newTextBlock(turn.step), text: ev.data.text });
      }
      return withPhase("generating", { blocks });
    }
    case "reasoning_delta": {
      // 思考独立成块，不混入正文；轮次变化时分别保存。仅存当前页面内存。
      const blocks = [...turn.blocks];
      const last = blocks[blocks.length - 1];
      if (last && last.type === "reasoning" && last.step === ev.data.step) {
        blocks[blocks.length - 1] = { ...last, text: last.text + ev.data.text };
      } else {
        blocks.push({ id: nextBlockId(), type: "reasoning", step: ev.data.step, text: ev.data.text });
      }
      return withPhase("analyzing", { blocks });
    }
    case "tool_started": {
      const { id, name, arguments: args } = ev.data;
      const tools = {
        ...turn.tools,
        [id]: turn.tools[id] ?? newToolEntity(id, name, args),
      };
      if (turn.tools[id]) return { ...turn, tools };
      return withPhase(turn.phase, {
        tools,
        blocks: [...turn.blocks, { id: nextBlockId(), type: "tool", callId: id }],
      });
    }
    case "tool_executing": {
      const entity = turn.tools[ev.data.id];
      if (!entity) return turn;
      if (entity.status === "denied") return turn;
      return withPhase(turn.phase, {
        tools: { ...turn.tools, [entity.id]: { ...entity, status: "running" } },
      });
    }
    case "tool_finished": {
      const entity = turn.tools[ev.data.id];
      if (!entity) return turn;
      // 审批拒绝后回填的"正常返回"不得覆盖为业务成功
      const status = entity.status === "denied" ? "denied" : resultStatus(ev.data);
      return {
        ...turn,
        tools: {
          ...turn.tools,
          [entity.id]: { ...entity, status, finished: ev.data, display: ev.data.display ?? null },
        },
      };
    }
    case "approval_pending": {
      const pending: ApprovalPendingPayload = ev.data;
      const existing = turn.tools[pending.call_id];
      const entity: ToolEntity = (existing ?? newToolEntity(pending.call_id, pending.tool,
        JSON.stringify(pending.arguments)));
      const blocks = existing
        ? turn.blocks
        : [...turn.blocks, { id: nextBlockId(), type: "tool" as const, callId: pending.call_id }];
      return withPhase("awaiting_approval", {
        blocks,
        tools: {
          ...turn.tools,
          [entity.id]: {
            ...entity,
            status: "waiting_approval",
            approval: pending,
            arguments: JSON.stringify(pending.arguments),
          },
        },
      });
    }
    case "approval_resolved": {
      const resolved: ApprovalResolvedPayload = ev.data;
      const entity = turn.tools[resolved.call_id];
      if (!entity) return turn;
      // 拒绝后轮次不再等待用户；批准则进入"已批准，等待执行"
      const phase: TurnPhase = resolved.approved
        ? "approved"
        : turn.phase === "awaiting_approval" ? "started" : turn.phase;
      return withPhase(phase, {
        tools: {
          ...turn.tools,
          [entity.id]: { ...entity, status: resolved.approved ? "prepared" : "denied", approvalResolved: resolved },
        },
      });
    }
    case "heartbeat":
      return turn;  // 连接存活信号，不改变任何内容
    case "done":
      return withPhase(ev.data.completed ? "completed" : "max_steps", { done: ev.data });
    case "error":
      return withPhase("failed", { error: ev.data.message });
  }
}

/** 流意外结束（非正常 done / 待审批挂起）：标记断连，保留已收到内容。 */
export function markDisconnected(turn: AssistantTurn, now = Date.now()): AssistantTurn {
  if (isTerminalPhase(turn.phase)) return turn;
  return {
    ...turn,
    phase: "disconnected",
    phaseAt: now,
    error: turn.error ?? "连接中断，本次回答可能不完整。",
  };
}

// ---- 展示辅助：阶段与工具的文案、已知工具名映射 ----

export function phaseLabel(turn: AssistantTurn): string {
  switch (turn.phase) {
    case "connecting": return "正在连接…";
    case "started": return "请求已接收";
    case "analyzing": return turn.blocks.at(-1)?.type === "reasoning"
      ? `正在思考 · 第 ${turn.step} 轮`
      : turn.step > 0 ? `正在分析请求 · 第 ${turn.step} 轮` : "正在分析请求";
    case "generating": return "正在生成回复";
    case "awaiting_approval": return "等待你的批准";
    case "approved": return "已批准，等待执行";
    case "completed": return "已完成";
    case "max_steps": return "达到执行轮次上限";
    case "failed": return "执行失败";
    case "disconnected": return "连接中断";
  }
}

const TOOL_TITLES: Record<string, string> = {
  get_order_status: "查询订单",
  get_order: "查询订单",
  list_orders: "查询订单列表",
  check_stock: "查询库存",
  get_stock: "查询库存",
  get_price: "查询价格",
  get_product: "查询商品",
  search_products: "搜索商品",
  compute_quote: "计算报价",
  adjust_stock: "调整库存",
  create_order: "创建订单",
  cancel_order: "取消订单",
  set_product_status: "商品上下架",
};

export function toolTitle(name: string): string {
  return TOOL_TITLES[name] ?? name;
}

/** 已验证参数的简短摘要（如 "A1001 · +9 件"）；未知字段不猜测。 */
export function toolSummary(entity: ToolEntity): string {
  let args: Record<string, unknown>;
  try {
    args = JSON.parse(entity.arguments) as Record<string, unknown>;
  } catch {
    return "";
  }
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const parts: string[] = [];
  const sku = args.sku;
  if (typeof sku === "string" && sku) parts.push(sku);
  const orderId = args.order_id;
  if (typeof orderId === "string" && orderId && !sku) parts.push(`订单 ${orderId}`);
  const delta = args.delta ?? args.quantity ?? args.amount;
  if (typeof delta === "number" && Number.isFinite(delta)) {
    parts.push(`${delta > 0 ? "+" : ""}${delta} 件`);
  }
  return parts.join(" · ");
}

export function toolStatusLabel(entity: ToolEntity): string {
  switch (entity.status) {
    case "prepared":
      return entity.approvalResolved?.approved ? "已批准，等待执行" : "已准备操作";
    case "waiting_approval": return "等待你的批准";
    case "running": return "正在执行操作";
    case "succeeded": return "已完成";
    case "failed": return "执行失败";
    case "denied": return "已拒绝，未执行";
    case "unknown": return "结果未知";
  }
}

export function canSendMessage(
  restoreState: RestoreState,
  sessionStatus: SessionState["status"] | null,
  pendingCount: number,
  busy: boolean,
): boolean {
  return restoreState !== "loading" && restoreState !== "recovery_failed" &&
    !busy && sessionStatus !== "waiting_approval" && sessionStatus !== "running" &&
    sessionStatus !== "interrupted" && pendingCount === 0;
}

// ---- 旧快照 → 有序轮次 ----

function resultStatus(result: ToolFinishedPayload): ToolStatus {
  let content: Record<string, unknown> | null = null;
  try {
    content = JSON.parse(result.content);
  } catch { /* Legacy tools may return plain text. */ }
  if (content?.approval === "denied") return "denied";
  if (!result.ok || content?.error != null) return "failed";
  const display = result.display;
  if (display?.version === 1 && ["succeeded", "failed", "denied", "unknown"].includes(display.outcome)) {
    return display.outcome;
  }
  switch (result.name) {
    case "check_stock":
    case "get_stock":
      return Number.isSafeInteger(content?.quantity ?? content?.stock) ? "succeeded" : "unknown";
    case "adjust_stock":
      return Number.isSafeInteger(content?.quantity) ? "succeeded" : "unknown";
    case "create_order":
      return typeof content?.order_id === "string" && content.order_id.length > 0 ? "succeeded" : "unknown";
  }
  return "succeeded";
}

/**
 * 把旧快照转换为有序轮次：优先使用展示投影 presentation（设计 5.3），
 * 缺失或版本不识别时回退 messages 转换。旧路径以 user 消息为分组边界；
 * 缺少原始事件时只恢复可确定的顺序，不补造耗时、审批过程或思考内容；
 * 已完成历史不带运行光标。
 */
export function hydrateTurns(state: SessionState): Turn[] {
  const turns = hydrateFromMessages(state);
  return hydrateFromPresentation(state, turns) ?? turns;
}

function hydrateFromPresentation(state: SessionState, history: Turn[]): Turn[] | null {
  const presentation = state.presentation;
  if (!presentation || presentation.version !== 1 || !Array.isArray(presentation.turns) ||
      presentation.turns.length === 0) {
    return null;
  }
  if (history.length === 0) {
    return presentation.turns.flatMap(turn => [
      { role: "user" as const, text: turn.user_text }, assistantFromPresentation(turn),
    ]);
  }
  // Keep the complete checkpoint history; journals may cover only recent runs.
  const turns = [...history];
  let before = turns.length;
  for (const projected of [...presentation.turns].reverse()) {
    let index = typeof projected.user_index === "number" ? projected.user_index * 2 : -1;
    const atIndex = turns[index];
    if (index >= before || atIndex?.role !== "user" || atIndex.text !== projected.user_text) {
      index = -1;
      // Old journals lack a message index. Match in order, from newest to oldest.
      for (let i = before - 2; i >= 0; i -= 2) {
        const user = turns[i];
        if (user.role === "user" && user.text === projected.user_text) { index = i; break; }
      }
    }
    if (index < 0) continue;
    before = index;
    const committed = turns[index + 1];
    if (isAssistantTurn(committed)) {
      turns[index + 1] = mergePresentation(committed, projected, state, index + 2 === turns.length);
    }
  }
  return turns;
}

function mergePresentation(
  committed: AssistantTurn, projected: PresentationTurn, state: SessionState, latest: boolean,
): AssistantTurn {
  const journal = assistantFromPresentation(projected);
  const active = latest && state.status !== "completed";
  const signature = (blocks: MessageBlock[]) => JSON.stringify(blocks.map(block =>
    block.type === "tool" ? { type: "tool", callId: block.callId }
      : { type: block.type, text: block.text }));
  const agrees = signature(committed.blocks) === signature(journal.blocks);
  const coversHistory = committed.blocks.every((block, index) => {
    const recorded = journal.blocks[index];
    return recorded?.type === block.type && (block.type === "tool"
      ? recorded.type === "tool" && recorded.callId === block.callId
      : recorded.type !== "tool" && recorded.text.startsWith(block.text));
  });
  const turn = { ...committed, blocks: (active && coversHistory) || agrees ? journal.blocks : committed.blocks,
    tools: { ...journal.tools, ...committed.tools } };
  for (const [id, entity] of Object.entries(turn.tools)) {
    const recorded = journal.tools[id];
    const actual = committed.tools[id];
    const pending = latest ? state.pending_approvals.find(p => p.call_id === id) : undefined;
    turn.tools[id] = {
      ...entity,
      display: actual?.finished && actual.finished.content === recorded?.finished?.content
        ? recorded.display : actual?.display ?? null,
      approval: pending ?? null,
      approvalResolved: actual?.finished ? recorded?.approvalResolved ?? null : null,
      status: pending ? "waiting_approval" : actual?.finished ? actual.status
        : active ? entity.status === "waiting_approval" ? "prepared" : entity.status : "unknown",
      arguments: pending ? JSON.stringify(pending.arguments) : entity.arguments,
    };
    if (!pending && actual?.finished) {
      turn.tools[id].status = actual.status === "denied" ? "denied"
        : resultStatus({ ...actual.finished, display: turn.tools[id].display ?? undefined });
    }
  }
  if (!active) {
    turn.done = agrees ? journal.done : null;
    turn.phase = turn.done?.completed === false ? "max_steps" : "completed";
    turn.error = null;
  } else if (state.status === "waiting_approval") {
    turn.phase = "awaiting_approval";
  } else if (state.status === "running") {
    turn.phase = turn.blocks.at(-1)?.type === "text" ? "generating" : "analyzing";
    turn.error = null;
  } else {
    turn.phase = "disconnected";
    turn.error = "任务中断，可继续恢复。";
  }
  return turn;
}

function assistantFromPresentation(turn: PresentationTurn): AssistantTurn {
  const assistant = newAssistantTurn();
  assistant.done = turn.done ?? null;
  assistant.error = turn.error ?? null;
  for (const block of turn.blocks) {
    if (block.type === "text") {
      assistant.blocks.push({ id: nextBlockId(), type: "text", step: 0, text: block.text });
      continue;
    }
    const entity = turn.tools[block.callId];
    if (!entity) continue;
    assistant.tools[block.callId] = {
      id: block.callId,
      name: entity.name,
      arguments: entity.arguments,
      status: entity.status,
      finished: entity.finished,
      display: entity.finished?.display ?? null,
      approval: entity.approval,
      approvalResolved: entity.approvalResolved,
    };
    assistant.blocks.push({ id: nextBlockId(), type: "tool", callId: block.callId });
  }
  const awaiting = Object.values(assistant.tools)
    .some(entity => entity.status === "waiting_approval");
  if (awaiting) {
    assistant.phase = "awaiting_approval";
  } else if (assistant.done) {
    assistant.phase = assistant.done.completed ? "completed" : "max_steps";
  } else if (assistant.error) {
    assistant.phase = "failed";
  } else {
    // 投影未收口：运行中未提交的文本可能丢失，不宣称逐 token 恢复（设计 5.3）
    assistant.phase = "disconnected";
    assistant.error = assistant.error ?? "部分输出未保存。";
  }
  return assistant;
}

function hydrateFromMessages(state: SessionState): Turn[] {
  const turns: Turn[] = [];
  let turn: AssistantTurn | null = null;
  for (const message of state.messages) {
    if (message.role === "user") {
      turns.push({ role: "user", text: message.content ?? "" });
      turn = newAssistantTurn();
      turns.push(turn);
      continue;
    }
    if (message.role === "tool" && turn && message.tool_call_id) {
      const entity = turn.tools[message.tool_call_id];
      if (entity) {
        entity.finished = { id: entity.id, name: entity.name, content: message.content ?? "", ok: true };
        entity.status = resultStatus(entity.finished);
      }
      continue;
    }
    if (message.role !== "assistant") continue;
    if (!turn) { turn = newAssistantTurn(); turns.push(turn); }
    if (message.content) {
      turn.blocks.push({ id: nextBlockId(), type: "text", step: 0, text: message.content });
    }
    for (const call of message.tool_calls ?? []) {
      turn.tools[call.id] = newToolEntity(call.id, call.function.name, call.function.arguments);
      turn.blocks.push({ id: nextBlockId(), type: "tool", callId: call.id });
    }
  }
  for (const result of state.tool_results) {
    for (const turn of turns) {
      if (!isAssistantTurn(turn)) continue;
      const entity = turn.tools[result.call_id];
      if (entity) {
        entity.finished = {
          id: result.call_id, name: result.name, content: result.content, ok: result.ok,
          display: result.display,
        };
        entity.display = entity.finished.display ?? null;
        entity.status = resultStatus(entity.finished);
      }
    }
  }
  for (const pending of state.pending_approvals) {
    for (const turn of [...turns].reverse()) {
      if (!isAssistantTurn(turn)) continue;
      const entity = Object.values(turn.tools)
        .find(item => item.id === pending.call_id && !entityResolved(item));
      if (entity) {
        entity.approval = pending;
        entity.status = "waiting_approval";
        entity.arguments = JSON.stringify(pending.arguments);
        turn.phase = "awaiting_approval";
        break;
      }
    }
  }
  // 历史轮次一律按已收口展示；等待审批的轮次由上方循环标出
  for (const turn of turns) {
    if (isAssistantTurn(turn) && turn.phase === "connecting") turn.phase = "completed";
  }
  const last = turns.at(-1);
  if (last && isAssistantTurn(last)) {
    if (state.status === "running") last.phase = "analyzing";
    if (state.status === "interrupted") {
      last.phase = "disconnected";
      last.error = "任务中断，可继续恢复。";
    }
  }
  return turns;
}

function entityResolved(entity: ToolEntity): boolean {
  return entity.status !== "prepared" || entity.approvalResolved !== null;
}
