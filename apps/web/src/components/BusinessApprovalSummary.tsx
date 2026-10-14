import { Alert, Button, Table, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ApprovalPendingPayload } from '../protocol';
import { approvalOperationIssue, type BusinessOperation } from '../operations';

export function BusinessApprovalSummary({ pending, operation, verified, enabled, loading, error, now, submitting, onRespond, onRefresh }: {
  pending: ApprovalPendingPayload; operation?: BusinessOperation; verified: boolean; enabled: boolean; loading: boolean; error?: string;
  now: number; submitting: boolean; onRespond: (id: string, approved: boolean) => void; onRefresh: () => void;
}) {
  const issue = !enabled ? '当前尚未开放此业务审批，请拒绝后重新准备。' : error || approvalOperationIssue(pending, operation, verified, now);
  return <section className="approval business-approval" aria-label={pending.tool === 'cancel_order' ? '取消订单审批' : '库存调整审批'}>
    <span className="approval-label">待审批 · {pending.tool === 'cancel_order' ? '取消订单' : '库存调整'}</span>
    <Tag color="warning">需要确认</Tag>
    {operation?.kind === 'cancel_order' && <>
      <p>订单 <strong>{operation.summary.order_id}</strong> · 客户 {operation.summary.customer}</p>
      <p>{'状态 ' + operation.summary.status_before + ' → ' + operation.summary.status_after + ' · 原单金额 ¥' + operation.summary.total_amount.toFixed(2)}</p>
      {operation.summary.note && <p>备注：{operation.summary.note}</p>}
      <p>取消订单不执行退款。</p>
      <div className="business-table" role="region" aria-label="取消后的库存回补"><Table size="small" pagination={false}
        rowKey={line => line.sku + '-' + line.name} dataSource={operation.summary.items} scroll={{ x: 570 }} columns={[
          { title: '商品', key: 'sku', width: 150, render: (_, line) => line.name + ' · ' + line.sku },
          { title: '数量', dataIndex: 'quantity', width: 55 },
          { title: '原单价格', key: 'price', render: (_, line) => '¥' + line.unit_price.toFixed(2) + ' · 合计 ¥' + line.line_total.toFixed(2) },
          { title: '库存变化 / 仓库', key: 'stock', render: (_, line) => line.stock_before === null ? '无库存记录，无法回补' : line.stock_before + ' → ' + line.stock_after + ' · ' + line.warehouse },
        ]} /></div>
    </>}
    {operation?.kind === 'adjust_stock' && <>
      <p>商品 <strong>{operation.summary.name}</strong> · {operation.summary.sku} · {operation.summary.warehouse}</p>
      <p>库存 {operation.summary.quantity_before} → <strong>{operation.summary.quantity_after}</strong> 件 · {operation.summary.delta >= 0 ? '增加' : '减少'} {Math.abs(operation.summary.delta)} 件</p>
      {!operation.summary.stock_exists && <p>当前无库存记录，将创建主仓库存记录。</p>}
    </>}
    {issue && <Alert type="warning" showIcon title={issue} />}
    <div className="approval-actions"><Button type="primary" className="approve" disabled={!!issue || loading || submitting} onClick={() => onRespond(pending.pending_id, true)}>批准</Button>
      <Button className="deny" disabled={submitting} onClick={() => onRespond(pending.pending_id, false)}>拒绝</Button>
      <Button icon={<ReloadOutlined />} disabled={loading || submitting} onClick={onRefresh}>{loading ? '正在核对…' : '刷新审批明细'}</Button></div>
    <details className="business-technical"><summary>技术详情</summary><pre className="args">{JSON.stringify(pending.arguments, null, 2)}</pre></details>
  </section>;
}
