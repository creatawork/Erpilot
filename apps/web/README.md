# web

React + TypeScript + Vite SPA（M1 第 4 周初始化）。

M1–M5 阶段只手写最小 UI：流式对话 + 工具时间线，不引组件库，
目的是理解 agent ↔ 前端的事件协议（为 AG-UI / generative UI 打底）。

M6 在 assistant-ui 与 CopilotKit 二选一后，再引入组件库实现审批卡片。

## 运行

```bash
# 先启动后端（仓库根目录）：
uv run --package erpilot-api uvicorn erpilot_api.main:app --reload

# 再起前端（vite 把 /api 代理到 127.0.0.1:8000）：
npm install
npm run dev        # http://localhost:5173
npm run build      # tsc --noEmit 类型检查 + vite 构建
```

## 事件协议

SSE 事件 v1：`start / step / delta / tool_started / tool_finished / done / error`，
字段定义见后端 `apps/api/src/erpilot_api/events.py` 的模块 docstring；
TypeScript 镜像类型在本包 `src/protocol.ts`——**两侧必须同步修改**。

实现要点：`EventSource` 只支持 GET，这里用 `fetch` + `ReadableStream` 手解
SSE（见 `src/protocol.ts` 的 `streamChat()`），POST 携带消息与会话 id。
