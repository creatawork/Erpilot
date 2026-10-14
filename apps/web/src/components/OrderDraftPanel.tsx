import { useEffect, useState } from 'react';
import { Alert, Button, Input, Select, Table, Tag, Tooltip } from 'antd';
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { type DraftInput, type OrderDraft } from '../drafts';

const money = (value: number) => '¥' + value.toFixed(2);
const statuses = { open: '待提交', expired: '已过期', superseded: '旧版本', submitted: '已提交' };
export function DraftSummary({ draft }: { draft: OrderDraft }) {
  return <div className="draft-summary">
    <p>{draft.customer} · 版本 {draft.revision} · <Tag>{statuses[draft.status]}</Tag></p>
    <div className="draft-table" role="region" aria-label="报价与库存影响">
      <Table size="small" pagination={false} rowKey="sku" dataSource={draft.items} scroll={{ x: 660 }} columns={[
        { title: '商品 / SKU', key: 'name', width: 160, render: (_, line) => <span>{line.name}<br /><span className="secondary-text">{line.sku}</span></span> },
        { title: '数量', dataIndex: 'quantity', width: 55 },
        { title: '原价', dataIndex: 'base_price', render: money },
        { title: '折扣', dataIndex: 'discount', render: value => Math.round(value * 100) + '%' },
        { title: '单价', dataIndex: 'unit_price', render: money },
        { title: '小计', dataIndex: 'line_total', render: money },
        { title: '库存前 → 后', key: 'stock', width: 110, render: (_, line) => line.stock_before + ' → ' + line.stock_after },
      ]} />
    </div>
    <p><strong>总金额 {money(draft.total_amount)}</strong></p>
    <p className="secondary-text">报价有效至 <time dateTime={draft.expires_at}>{new Date(draft.expires_at).toLocaleString('zh-CN')}</time></p>
    {draft.note && <p>备注：{draft.note}</p>}{draft.order_id && <p>订单：{draft.order_id}</p>}
  </div>;
}
export function OrderDraftPanel({ drafts, loading, error, busy, canSubmit, writesEnabled = true, hasPending, onRefresh, onSave, onSubmit }: {
  drafts: OrderDraft[]; loading: boolean; error: string; busy: boolean; canSubmit: boolean; writesEnabled?: boolean; hasPending: boolean;
  onRefresh: () => void; onSave: (draft: OrderDraft | undefined, input: DraftInput) => Promise<OrderDraft | void>; onSubmit: (draft: OrderDraft) => void;
}) {
  const [selected, setSelected] = useState<string | null | undefined>(undefined);
  const draft = selected === null ? undefined : drafts.find(item => item.draft_id === selected) ?? drafts[0];
  const [customer, setCustomer] = useState(''); const [note, setNote] = useState('');
  const [lines, setLines] = useState([{ sku: '', quantity: '1' }]);
  const [saving, setSaving] = useState(false); const [saveError, setSaveError] = useState('');
  useEffect(() => {
    setCustomer(draft?.customer ?? ''); setNote(draft?.note ?? '');
    setLines(draft?.items.map(item => ({ sku: item.sku, quantity: String(item.quantity) })) ?? [{ sku: '', quantity: '1' }]); setSaveError('');
  }, [draft?.draft_id, draft?.revision]);
  const original = draft ? JSON.stringify({ customer: draft.customer, note: draft.note ?? '', items: draft.items.map(item => ({ sku: item.sku, quantity: String(item.quantity) })) }) : '';
  const dirty = original !== JSON.stringify({ customer, note, items: lines });
  const valid = !!customer.trim() && lines.length > 0 && lines.every(line => !!line.sku.trim() && Number.isSafeInteger(Number(line.quantity)) && Number(line.quantity) > 0);
  const locked = busy || saving || draft?.status === 'submitted';
  return <section className="draft-panel" aria-label="订单草稿" aria-busy={loading || saving}>
    <div className="draft-heading"><h2>准备订单</h2><div>
      <Button size="small" icon={<PlusOutlined />} onClick={() => { setSelected(null); setCustomer(''); setNote(''); setLines([{ sku: '', quantity: '1' }]); setSaveError(''); }} disabled={busy || saving}>新建草稿</Button>
      <Tooltip title="刷新草稿"><Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新草稿" onClick={onRefresh} disabled={loading || saving} /></Tooltip>
    </div></div>
    {drafts.length > 0 && <label>选择草稿<Select aria-label="选择草稿" value={draft?.draft_id ?? ''} onChange={value => setSelected(value || null)} disabled={saving}
      options={[{ value: '', label: '新草稿' }, ...drafts.map(item => ({ value: item.draft_id, label: item.customer + ' · 版本 ' + item.revision }))]} /></label>}
    {loading && <p className="secondary-text" role="status">正在核对草稿…</p>}{error && <Alert type="error" title={error} showIcon />}
    <form onSubmit={async event => {
      event.preventDefault(); if (!valid || locked) return; setSaving(true); setSaveError('');
      try { const saved = await onSave(draft, { customer: customer.trim(), note, items: lines.map(line => ({ sku: line.sku.trim(), quantity: Number(line.quantity) })) }); if (saved) setSelected(saved.draft_id); }
      catch (error) { setSaveError(error instanceof Error ? error.message : String(error)); } finally { setSaving(false); }
    }}>
      <label>客户<Input aria-label="草稿客户" placeholder="输入客户名称" value={customer} onChange={event => setCustomer(event.target.value)} disabled={locked} required /></label>
      {lines.map((line, index) => <div className="draft-line" key={index}>
        <label>商品 SKU<Input aria-label={'第 ' + (index + 1) + ' 行 SKU'} placeholder="商品编码" value={line.sku} onChange={event => setLines(prev => prev.map((item, i) => i === index ? { ...item, sku: event.target.value } : item))} disabled={locked} required /></label>
        <label>数量<Input aria-label={'第 ' + (index + 1) + ' 行数量'} type="number" min="1" step="1" value={line.quantity} onChange={event => setLines(prev => prev.map((item, i) => i === index ? { ...item, quantity: event.target.value } : item))} disabled={locked} required /></label>
        <Button type="text" icon={<DeleteOutlined />} aria-label={'删除第 ' + (index + 1) + ' 行'} onClick={() => setLines(prev => prev.filter((_, i) => i !== index))} disabled={locked || lines.length === 1} />
      </div>)}
      <label>备注<Input.TextArea aria-label="草稿备注" placeholder="补充订单要求（选填）" value={note} onChange={event => setNote(event.target.value)} disabled={locked} autoSize={{ minRows: 2, maxRows: 4 }} /></label>
      <div className="draft-actions"><Button icon={<PlusOutlined />} onClick={() => setLines(prev => [...prev, { sku: '', quantity: '1' }])} disabled={locked}>添加商品</Button>
        <Button type="primary" htmlType="submit" loading={saving} disabled={locked || !valid}>{draft ? '保存 / 刷新报价' : '创建草稿'}</Button></div>
    </form>
    {saveError && <Alert type="error" title={saveError} showIcon />}
    {draft && <><DraftSummary draft={draft} /><Button className="draft-submit" disabled={!writesEnabled || !canSubmit || saving || dirty || draft.status !== 'open' || Date.parse(draft.expires_at) <= Date.now()} onClick={() => onSubmit(draft)}>请求提交版本 {draft.revision} 并审批</Button></>}
    {!writesEnabled && <p className="secondary-text" role="status">草稿可保存，下单未开放。</p>}
    {dirty && draft && <p className="secondary-text" role="status">修改后请先保存，以新的报价版本申请审批。</p>}
    {hasPending && <p className="secondary-text" role="status">已有待处理审批；修改草稿后，请先拒绝旧审批，再提交新版本。</p>}
  </section>;
}
