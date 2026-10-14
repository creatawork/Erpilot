import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Skeleton, Tooltip } from 'antd';
import { Conversations } from '@ant-design/x';
import { LeftOutlined, MessageOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { historyStatusLabel, loadSessionPage, type SessionPage } from '../history';

export function SessionHistory({ currentId, locked, onSelect, onClose }: {
  currentId: string; locked: boolean; onSelect: (id: string) => void; onClose?: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<SessionPage>({ items: [], has_more: false });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const request = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    const generation = ++request.current;
    setLoading(true); setError(''); setPage({ items: [], has_more: false });
    void loadSessionPage(offset, controller.signal).then(result => {
      if (!controller.signal.aborted && generation === request.current) setPage(result);
    }).catch(error => {
      if (!controller.signal.aborted && generation === request.current) setError(error instanceof Error ? error.message : String(error));
    }).finally(() => {
      if (!controller.signal.aborted && generation === request.current) setLoading(false);
    });
    return () => controller.abort();
  }, [offset, revision, currentId]);
  return <section className="session-history" aria-labelledby="history-heading" aria-busy={loading}>
    <div className="history-heading">
      <h2 id="history-heading">历史会话</h2>
      <Tooltip title="刷新列表"><Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新列表"
        onClick={() => setRevision(value => value + 1)} disabled={loading} /></Tooltip>
      {onClose && <Button type="text" size="small" onClick={onClose}>关闭历史</Button>}
    </div>
    {locked && <p className="history-notice" role="status">任务执行中，结束后可切换会话。</p>}
    {loading ? <Skeleton active paragraph={{ rows: 4 }} title={false} /> : error ?
      <Alert type="error" title="历史会话加载失败" description={error} showIcon
        action={<Button size="small" onClick={() => setRevision(value => value + 1)}>重试</Button>} /> :
      page.items.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史会话" /> :
      <Conversations activeKey={currentId} onActiveChange={key => { if (!locked) onSelect(key); }}
        items={page.items.map(item => ({
          key: item.session_id, disabled: locked, icon: <MessageOutlined />,
          label: <div className="history-item-label">
            <span className="history-title">{item.title || '未命名会话'}</span>
            <span className="history-meta"><time dateTime={item.updated_at}>
              {new Date(item.updated_at).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })}
            </time><span>{historyStatusLabel(item.status)}</span></span>
          </div>,
          'aria-label': item.title || '未命名会话',
          'aria-current': item.session_id === currentId ? 'true' as const : undefined,
        }))} />}
    {(offset > 0 || page.has_more) && <nav aria-label="历史会话分页" className="history-pagination">
      <Button size="small" icon={<LeftOutlined />} aria-label="上一页" disabled={loading || offset === 0}
        onClick={() => setOffset(value => Math.max(0, value - 20))} />
      <span>第 {offset / 20 + 1} 页</span>
      <Button size="small" icon={<RightOutlined />} aria-label="下一页" disabled={loading || !page.has_more}
        onClick={() => setOffset(value => value + 20)} />
    </nav>}
  </section>;
}
