# web

React + TypeScript + Vite SPA，使用 Ant Design 6、Ant Design X 2 和 X Markdown。

## 工作台

左侧会话历史，中间流式对话，右侧按需展示订单草稿、审批和业务结果。窄屏通过抽屉访问历史与业务面板。主题集中在 src/theme.ts，布局在 WorkspaceShell.tsx 和 App.css。

保留自有 SSE 和会话恢复协议，不引入新的 Agent 后端。订单报价、库存影响和审批内容使用服务端快照；恢复失败、待审批或未完成任务时锁定新消息，避免重复写入。模型 Markdown 转义原始 HTML，并过滤危险链接。

## 运行

先在仓库根目录运行 uv run --package erpilot-api uvicorn erpilot_api.main:app --reload，再在 apps/web 运行 npm install 和 npm run dev。Vite 将 /api 代理到 127.0.0.1:8000。

npm run build 执行类型检查和生产构建；npm test 执行协议和组件回归。安装浏览器：npx playwright install chromium；运行页面验收：npm run test:ui。可通过 PLAYWRIGHT_CHROMIUM_EXECUTABLE 指定已安装的 Chromium。

浏览器测试使用模拟 API，覆盖桌面/手机布局、键盘发送、Markdown 安全、审批锁定和历史恢复失败，不执行真实 ERP 写入。

## 事件协议

fetch + ReadableStream 解析 POST SSE，类型和解析见 src/protocol.ts，恢复与状态处理见 src/session.ts。后端协议变更须同步修改两侧定义。

## 已知限制

组件库增加了首屏资源量，目前生产入口约 413 KB gzip；后续功能增长时应按业务区域拆分加载。依赖审计剩余 KaTeX 相关低风险上游告警（Ant Design X / X Markdown 的间接依赖），目前无兼容修复；未为消除告警强制降级组件库。
