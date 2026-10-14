import type { ReactNode } from 'react';
import { Button, Drawer, Grid, Layout, Tooltip } from 'antd';
import { AppstoreOutlined, CloseOutlined, MenuOutlined, PlusOutlined } from '@ant-design/icons';

export function WorkspaceShell({ sidebar, children, composer, business, runtimeLabel, status, historyOpen,
  businessOpen, sessionLocked, onHistoryOpen, onBusinessOpen, onNewSession }: {
  sidebar: ReactNode; children: ReactNode; composer: ReactNode; business: ReactNode;
  runtimeLabel: string; status: string; historyOpen: boolean; businessOpen: boolean; sessionLocked: boolean;
  onHistoryOpen: (open: boolean) => void; onBusinessOpen: (open: boolean) => void; onNewSession: () => void;
}) {
  const screens = Grid.useBreakpoint();
  const sidebarContent = <div className="sidebar-inner">
    <a className="brand" href="#main-content" aria-label="Erpilot 掌柜助手，跳到对话">
      <span className="brand-mark">e</span><span>Erpilot<small>掌柜助手</small></span>
    </a>
    <Button className="new-conversation" aria-label="新会话" icon={<PlusOutlined />} onClick={onNewSession} disabled={sessionLocked}>新会话</Button>
    {sidebar}
    <div className="sidebar-bottom"><span className="workspace-avatar">掌</span><div><strong>业务工作台</strong><p>{runtimeLabel}</p></div></div>
  </div>;
  return <Layout className="app workspace">
    <a className="skip-link" href="#main-content">跳到对话</a>
    {screens.lg && <Layout.Sider className="workspace-sidebar" width={248}>{sidebarContent}</Layout.Sider>}
    <Layout className="workspace-content">
      <header className="workspace-header">
        <div className="header-title">
          {!screens.lg && <Button type="text" icon={<MenuOutlined />} aria-label="历史会话" onClick={() => onHistoryOpen(true)} />}
          <div><h1>掌柜助手</h1><p>订单、库存与报价，一起处理。</p></div>
        </div>
        <div className="header-actions">
          <span className="runtime-label">{runtimeLabel}</span>
          <Tooltip title={businessOpen ? '收起业务面板' : '查看订单、审批和结果'}>
            <Button icon={<AppstoreOutlined />} aria-label="业务面板" aria-expanded={businessOpen}
              onClick={() => onBusinessOpen(!businessOpen)} className={businessOpen ? 'panel-toggle active' : 'panel-toggle'}>业务面板</Button>
          </Tooltip>
        </div>
      </header>
      <div className="workspace-body">
        <main className="chat-main" id="main-content" tabIndex={-1}>
          {children}
          <div className="composer-area">{status && <p className="session-status" role="status">{status}</p>}{composer}</div>
        </main>
        {screens.xl && businessOpen && <aside className="business-pane" aria-label="业务面板">
          <div className="business-pane-heading"><h2>业务面板</h2><Button type="text" size="small" icon={<CloseOutlined />}
            aria-label="关闭业务面板" onClick={() => onBusinessOpen(false)} /></div>{business}
        </aside>}
      </div>
    </Layout>
    {!screens.lg && <Drawer title="会话" placement="left" open={historyOpen} onClose={() => onHistoryOpen(false)}
      size={280} className="history-drawer">{sidebarContent}</Drawer>}
    {!screens.xl && <Drawer title="业务面板" placement="right" open={businessOpen} onClose={() => onBusinessOpen(false)}
      size={460} className="business-drawer">{business}</Drawer>}
  </Layout>;
}
