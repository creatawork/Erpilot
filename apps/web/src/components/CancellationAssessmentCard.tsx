import { Card, Tag } from 'antd';
import type { CancellationAssessmentDisplay } from '../protocol';

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}
function time(value: unknown): value is string {
  return typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value));
}
function number(value: unknown, low: number, high = Infinity): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high;
}

export function isUsableAssessmentDisplay(value: unknown): value is CancellationAssessmentDisplay {
  if (!record(value) || value.version !== 1 || value.kind !== 'cancellation_assessment' ||
      value.outcome !== 'succeeded') return false;
  const proof = value.assessment;
  if (!record(proof) || proof.schema_version !== 1 || !time(proof.assessed_at) ||
      !text(proof.message) || typeof proof.business_allowed !== 'boolean') return false;
  const order = proof.order;
  if (!record(order) || !text(order.order_id) || !text(order.customer) || !text(order.status) ||
      !number(order.total_amount, 0) ||
      !['待付款','待发货','已发货','已签收','已取消','已退款'].includes(order.status)) return false;
  if (proof.business_allowed !== ['待付款','待发货'].includes(order.status)) return false;
  const reason = proof.business_reason;
  if (!record(reason) || !text(reason.message) ||
      reason.code !== (proof.business_allowed ? 'cancellable_status' : 'invalid_transition')) return false;
  if (typeof proof.policy_state !== 'string' ||
      !['matched','no_match','unavailable','inactive','conflict'].includes(proof.policy_state)) return false;
  const expected = !proof.business_allowed ? 'ineligible' :
    proof.policy_state === 'matched' ? 'eligible' : 'needs_review';
  if (proof.decision !== expected || !Array.isArray(proof.references) ||
      (proof.references.length > 0) !== (proof.policy_state === 'matched')) return false;
  const versions = new Map<string, string>();
  for (const citation of proof.references) {
    if (!record(citation) || !text(citation.source) || !text(citation.title) || !text(citation.text) ||
        !text(citation.policy_id) || !text(citation.version) || !number(citation.score, -1, 1) ||
        !time(citation.effective_from) || Date.parse(citation.effective_from) > Date.parse(proof.assessed_at)) return false;
    if (citation.effective_until !== null && (!time(citation.effective_until) ||
        Date.parse(citation.effective_until) <= Date.parse(proof.assessed_at))) return false;
    const version = JSON.stringify([citation.version, citation.effective_from, citation.effective_until]);
    if (versions.has(citation.policy_id) && versions.get(citation.policy_id) !== version) return false;
    versions.set(citation.policy_id, version);
  }
  return true;
}

const labels = {eligible: '可发起取消申请', ineligible: '当前不可取消', needs_review: '政策待核对'};
const policyLabels = {matched: '政策依据有效', no_match: '未找到相关依据', unavailable: '政策检索暂不可用',
  inactive: '没有当前生效政策', conflict: '政策版本或规则冲突'};

export function CancellationAssessmentCard({display}: {display: CancellationAssessmentDisplay}) {
  if (!isUsableAssessmentDisplay(display)) return null;
  const proof = display.assessment;
  return <Card size="small" className="card cancellation-assessment" title="取消条件查询"
    extra={<Tag>查询完成</Tag>}>
    <div className="card-body">
      <Tag color={proof.decision === 'eligible' ? 'processing' : proof.decision === 'needs_review' ? 'warning' : undefined}>
        {labels[proof.decision]}</Tag>
      <p>订单 <strong>{proof.order.order_id}</strong> · {proof.order.customer} · {proof.order.status}</p>
      <p>原单金额 ¥{proof.order.total_amount.toFixed(2)}</p>
      <p>{proof.business_reason.message} · {policyLabels[proof.policy_state]}</p>
      <p>{proof.message}</p>
      <p>查询时间 <time dateTime={proof.assessed_at}>{proof.assessed_at}</time></p>
      {proof.references.length > 0 && <details><summary>查看政策依据</summary>
        {proof.references.map((citation, index) => <section key={index}>
          <p><strong>{citation.title}</strong> · {citation.source}</p>
          <p>政策 {citation.policy_id} · 版本 {citation.version}</p>
          <p>生效 {citation.effective_from} · {citation.effective_until ? `截至 ${citation.effective_until}` : '未设截止时间'}</p>
          <p>{citation.text}</p>
        </section>)}
      </details>}
    </div>
  </Card>;
}
