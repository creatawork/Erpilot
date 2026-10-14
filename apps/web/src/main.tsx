import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import App from './App';
import { workspaceTheme } from './theme';

createRoot(document.getElementById('root')!).render(
  <ConfigProvider locale={zhCN} theme={workspaceTheme} button={{ autoInsertSpace: false }}><App /></ConfigProvider>,
);
