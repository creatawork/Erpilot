import {useEffect, useRef, useState} from 'react';
import {historyStatusLabel, loadSessionPage, type SessionPage} from '../history';

export function SessionHistory({currentId, locked, onSelect, onClose}: {
  currentId: string; locked: boolean; onSelect: (id: string) => void; onClose: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<SessionPage>({items: [], has_more: false});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const heading = useRef<HTMLHeadingElement>(null);
  const request = useRef(0);
  useEffect(() => {heading.current?.focus();}, []);
  useEffect(() => {
    const controller = new AbortController();
    const generation = ++request.current;
    setLoading(true); setError(''); setPage({items: [], has_more: false});
    void loadSessionPage(offset, controller.signal).then(result => {
      if (!controller.signal.aborted && generation === request.current) setPage(result);
    }).catch(error => {
      if (!controller.signal.aborted && generation === request.current) setError(error instanceof Error ? error.message : String(error));
    }).finally(() => {if (!controller.signal.aborted && generation === request.current) setLoading(false);});
    return () => controller.abort();
  }, [offset, revision]);
  return <section className="session-history" aria-labelledby="history-heading" aria-busy={loading}>
    <div className="history-heading"><h2 id="history-heading" ref={heading} tabIndex={-1}>历史会话</h2>
      <button onClick={() => setRevision(value => value + 1)} disabled={loading}>刷新列表</button>
      <button onClick={onClose}>关闭历史</button></div>
    {locked && <p role="status">当前任务正在执行，结束后可切换会话。</p>}
    {loading ? <p role="status">正在加载历史会话…</p> : error ? <p role="alert">{error}，请刷新列表重试。</p> :
      page.items.length === 0 ? <p role="status">暂无历史会话。</p> : <ul className="history-list">
        {page.items.map(item => <li key={item.session_id}><button disabled={locked} aria-current={item.session_id === currentId ? 'true' : undefined}
          onClick={() => onSelect(item.session_id)}>
          <span className="history-title">{item.title || '未命名会话'}{item.session_id === currentId && <small> · 当前会话</small>}</span>
          <span className="history-meta"><time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleString('zh-CN')}</time>
            <span>{historyStatusLabel(item.status)}</span></span>
        </button></li>)}
      </ul>}
    <nav aria-label="历史会话分页" className="history-pagination"><button disabled={loading || offset === 0} onClick={() => setOffset(value => Math.max(0, value - 20))}>上一页</button>
      <span>第 {offset / 20 + 1} 页</span><button disabled={loading || !page.has_more} onClick={() => setOffset(value => value + 20)}>下一页</button></nav>
  </section>;
}
