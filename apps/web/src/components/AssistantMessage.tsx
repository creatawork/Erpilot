/**
 * 助手消息：进度区域 + 按收到顺序渲染的内容块（思考 / 正文 / 工具）。
 * 工具实体按 callId 保存，块只引用；并行调用按请求顺序展示，状态独立更新。
 * 有 display 的工具结果渲染业务卡片，契约不符回退通用详情。
 */
import type { ApprovalPendingPayload } from "../protocol";
import type { AssistantTurn, ToolEntity } from "../session";
import { toolStatusLabel, toolSummary, toolTitle } from "../session";
import { BusinessResultCard, isUsableDisplay } from "./BusinessResultCard";
import { isUsableAssessmentDisplay } from './CancellationAssessmentCard';
import { MarkdownContent } from "./MarkdownContent";
import { ReasoningPanel } from "./ReasoningPanel";
import { WorkTimeline } from "./WorkTimeline";
import type { ReactNode } from 'react';
import { Bubble } from '@ant-design/x';
import { Button, Tag } from 'antd';
import { CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined, LoadingOutlined, PauseCircleOutlined, QuestionCircleOutlined, StopOutlined, UndoOutlined } from '@ant-design/icons';
import { isBusinessApproval, requiresBusinessSummary } from '../operations';

const STATUS_ICON: Record<ToolEntity['status'], ReactNode> = {
  prepared: <ClockCircleOutlined />, waiting_approval: <PauseCircleOutlined />, running: <LoadingOutlined />,
  succeeded: <CheckCircleOutlined />, failed: <CloseCircleOutlined />, denied: <StopOutlined />,
  expired: <ClockCircleOutlined />, cancelled: <UndoOutlined />, unknown: <QuestionCircleOutlined />,
};

function ToolCard({
  entity,
  now,
  submittingApprovals,
  onRespond,
  renderDraftApproval,
  renderBusinessApproval,
  businessApprovalsEnabled = false,
  onInspectApproval,
}: {
  entity: ToolEntity;
  now: number;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
  renderDraftApproval?: (pending: ApprovalPendingPayload) => ReactNode;
  renderBusinessApproval?: (pending: ApprovalPendingPayload) => ReactNode;
  businessApprovalsEnabled?: boolean;
  onInspectApproval?: () => void;
}) {
  const summary = toolSummary(entity);
  return (
    <details className="tool" open={!isSettled(entity) || isUsableAssessmentDisplay(entity.display)}>
      <summary>
        <span className={`tool-icon ${entity.status}`}>{STATUS_ICON[entity.status]}</span>
        <span className="tool-name">{toolTitle(entity.name)}</span>
        {summary && <span className="tool-summary">{summary}</span>}
        <Tag className="tool-status" color={entity.status === "succeeded" ? "success" : entity.status === "failed" ? "error" : entity.status === "waiting_approval" ? "warning" : undefined}>{toolStatusLabel(entity)}</Tag>
      </summary>
      <details className="tool-technical"><summary>{isBusinessApproval(entity.name) ? "技术详情" : "调用参数"}</summary><pre className="args">{entity.arguments}</pre></details>
      {entity.approval && !entity.approvalResolved && (
        onInspectApproval ? <div className="tool-approval-link"><span>操作需要你确认</span><Button size="small" onClick={onInspectApproval}>查看审批</Button></div> :
        requiresBusinessSummary(entity.approval,businessApprovalsEnabled) && renderBusinessApproval ? renderBusinessApproval(entity.approval) : entity.approval.tool === 'submit_order_draft' && renderDraftApproval ? renderDraftApproval(entity.approval) : <ApprovalActions
          pending={entity.approval}
          businessApprovalsEnabled={businessApprovalsEnabled}
          now={now}
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
    entity.status === "denied" || entity.status === "expired" ||
    entity.status === "cancelled" || entity.status === "unknown";
}

function ApprovalActions({
  businessApprovalsEnabled,
  pending,
  now,
  submittingApprovals,
  onRespond,
}: {
  pending: ApprovalPendingPayload;
  businessApprovalsEnabled: boolean;
  now: number;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
}) {
  const expired = pending.expires_at != null && Date.parse(pending.expires_at) <= now;
  return (
    <div className="approval">
      <span className="approval-label">
        {expired ? "审批已过期" : "待审批"} · 风险等级 {pending.risk}
      </span>
      {expired ? (
        <Button className="deny" disabled={submittingApprovals.has(pending.pending_id)}
          onClick={() => onRespond(pending.pending_id, false)}>
          收口过期审批
        </Button>
      ) : (
        <>
          <Button
            type="primary" className="approve"
            disabled={submittingApprovals.has(pending.pending_id)||requiresBusinessSummary(pending,businessApprovalsEnabled)}
            onClick={() => onRespond(pending.pending_id, true)}
          >批准</Button>
          <Button
            className="deny"
            disabled={submittingApprovals.has(pending.pending_id)}
            onClick={() => onRespond(pending.pending_id, false)}
          >拒绝</Button>
        </>
      )}
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
  renderDraftApproval,
  renderBusinessApproval,
  businessApprovalsEnabled = false,
  onInspectApproval,
}: {
  turn: AssistantTurn;
  now: number;
  live: boolean;
  stalled: boolean;
  submittingApprovals: Set<string>;
  onRespond: (pendingId: string, approved: boolean) => void;
  renderDraftApproval?: (pending: ApprovalPendingPayload) => ReactNode;
  renderBusinessApproval?: (pending: ApprovalPendingPayload) => ReactNode;
  businessApprovalsEnabled?: boolean;
  onInspectApproval?: () => void;
}) {
  const showCursor = live && turn.phase === "generating";
  return (
    <Bubble className="assistant-message" variant="borderless" header={<span className="assistant-name">掌柜助手</span>}
      avatar={<span className="assistant-avatar">e</span>} content={<div className="assistant-body">
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
              <MarkdownContent text={block.text} streaming={showCursor && block === turn.blocks[turn.blocks.length - 1]} />
              {showCursor && block === turn.blocks[turn.blocks.length - 1] && (
                <span className="cursor" />
              )}
            </div>
          );
        }
        return (
          <ToolCard
            key={block.id}
            entity={turn.tools[block.callId] ?? fallbackToolEntity(block.callId)}
            now={now}
            submittingApprovals={submittingApprovals}
            onRespond={onRespond}
            renderDraftApproval={renderDraftApproval}
            renderBusinessApproval={renderBusinessApproval}
            businessApprovalsEnabled={businessApprovalsEnabled}
            onInspectApproval={onInspectApproval}
          />
        );
      })}
      {showCursor && turn.blocks.length === 0 && <span className="cursor" />}
    </div>} />
  );
}

function fallbackToolEntity(callId: string): ToolEntity {
  return {
    id: callId, name: callId, arguments: "", status: "unknown",
    finished: null, display: null, approval: null, approvalResolved: null,
  };
}
