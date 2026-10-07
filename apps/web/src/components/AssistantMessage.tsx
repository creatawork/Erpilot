/**
 * 助手消息：进度区域 + 按收到顺序渲染的内容块（思考 / 正文 / 工具）。
 * 工具实体按 callId 保存，块只引用；并行调用按请求顺序展示，状态独立更新。
 * 有 display 的工具结果渲染业务卡片，契约不符回退通用详情。
 */
import type { ApprovalPendingPayload } from "../protocol";
import type { AssistantTurn, ToolEntity } from "../session";
import { toolStatusLabel, toolSummary, toolTitle } from "../session";
import { BusinessResultCard, isUsableDisplay } from "./BusinessResultCard";
import { MarkdownContent } from "./MarkdownContent";
import { ReasoningPanel } from "./ReasoningPanel";
import { WorkTimeline } from "./WorkTimeline";

const STATUS_ICON: Record<ToolEntity["status"], string> = {
  prepared: "⋯",
  waiting_approval: "⏸",
  running: "⋯",
  succeeded: "✓",
  failed: "✗",
  denied: "⊘",
  unknown: "?",
};

function ToolCard({
  entity,
  submittingApprovals,
  onRespond,
}: {
  entity: ToolEntity;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
}) {
  const summary = toolSummary(entity);
  return (
    <details className="tool" open={!isSettled(entity)}>
      <summary>
        <span className={`tool-icon ${entity.status}`}>{STATUS_ICON[entity.status]}</span>
        <span className="tool-name">{toolTitle(entity.name)}</span>
        {summary && <span className="tool-summary">{summary}</span>}
        <span className="tool-status">{toolStatusLabel(entity)}</span>
      </summary>
      <pre className="args">{entity.arguments}</pre>
      {entity.approval && !entity.approvalResolved && (
        <ApprovalActions
          pending={entity.approval}
          submittingApprovals={submittingApprovals}
          onRespond={onRespond}
        />
      )}
      {entity.approvalResolved && (
        <div className={`approval ${entity.approvalResolved.approved ? "ok" : "fail"}`}>
          {entity.approvalResolved.approved
            ? "已批准"
            : `已拒绝：${entity.approvalResolved.reason || "无理由"}`}
        </div>
      )}
      {entity.finished && (isUsableDisplay(entity.display)
        ? <BusinessResultCard display={entity.display!} />
        : <pre className="result">{entity.finished.content}</pre>)}
    </details>
  );
}

function isSettled(entity: ToolEntity): boolean {
  return entity.status === "succeeded" || entity.status === "failed" ||
    entity.status === "denied" || entity.status === "unknown";
}

function ApprovalActions({
  pending,
  submittingApprovals,
  onRespond,
}: {
  pending: ApprovalPendingPayload;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
}) {
  return (
    <div className="approval">
      <span className="approval-label">
        待审批 · 风险等级 {pending.risk}
      </span>
      <button
        className="approve"
        disabled={submittingApprovals.has(pending.pending_id)}
        onClick={() => onRespond(pending.pending_id, true)}
      >
        批准
      </button>
      <button
        className="deny"
        disabled={submittingApprovals.has(pending.pending_id)}
        onClick={() => onRespond(pending.pending_id, false)}
      >
        拒绝
      </button>
    </div>
  );
}

export function AssistantMessage({
  turn,
  now,
  live,
  stalled,
  submittingApprovals,
  onRespond,
}: {
  turn: AssistantTurn;
  now: number;
  live: boolean;
  stalled: boolean;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
}) {
  const showCursor = live && turn.phase === "generating";
  return (
    <div className="bubble assistant">
      <WorkTimeline turn={turn} now={now} stalled={stalled} />
      {turn.blocks.map(block => {
        if (block.type === "reasoning") {
          return <ReasoningPanel key={block.id} step={block.step} text={block.text}
            live={live && (turn.phase === "analyzing" || turn.phase === "generating") &&
              block === turn.blocks[turn.blocks.length - 1]} />;
        }
        if (block.type === "text") {
          if (!block.text) return null;
          return (
            <div className="text" key={block.id}>
              <MarkdownContent text={block.text} />
              {showCursor && block === turn.blocks[turn.blocks.length - 1] && (
                <span className="cursor">▍</span>
              )}
            </div>
          );
        }
        return (
          <ToolCard
            key={block.id}
            entity={turn.tools[block.callId] ?? fallbackToolEntity(block.callId)}
            submittingApprovals={submittingApprovals}
            onRespond={onRespond}
          />
        );
      })}
      {showCursor && turn.blocks.length === 0 && <span className="cursor">▍</span>}
    </div>
  );
}

function fallbackToolEntity(callId: string): ToolEntity {
  return {
    id: callId, name: callId, arguments: "", status: "unknown",
    finished: null, display: null, approval: null, approvalResolved: null,
  };
}
