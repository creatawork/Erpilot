# B4 Task and History Implementation Plan

> For agentic workers: use executing-plans and dispatching-parallel-agents for separate domain/API and frontend contracts. Test before implementation; record verification.

**Goal:** 多轮草稿修改有结构化事实，旧会话可找回。
**Architecture:** 复用 ERP 草稿快照；MCP 增加查询与确定性数量修改；RunStore 只读历史列表；前端进入会话仍使用原恢复接口。
**Tech Stack:** SQLAlchemy、FastMCP、FastAPI、React、TypeScript。
**Spec:** ../specs/2026-10-11-task-history-design.md

## Global Constraints

Python >=3.12；无新依赖、不部署；agent_core 零 ERP 依赖；模型无 session_id；只提交 B4 文件。

## Review Focus

多个草稿不能猜最新；21 个候选不能误判唯一；旧版本行号不能修改新版本；切换后旧响应及错误不能串会话；历史状态不能触发执行。

## Task 1：持久草稿查询和行修改

Files: erp_store/drafts.py、mcp_erp/draft_tools.py，对应测试。
- [x] 写失败测试：`assert drafts.task_state('s1')['status']=='none'`；准备唯一、多份、21 份草稿，断言 active/ambiguous 和 has_more。
- [x] 查询所有未提交最新版本以判定唯一，返回最多 20 候选。`revise_quantity(session_id,draft_id,expected_revision,item_number,quantity)` 在写锁内验证并复用快照生成新版本。
- [x] 测试第 2 行改成 50，客户/备注/第 1 行不变；旧版本、越界、外会话和提交草稿拒绝；重建服务得到同一事实。
- [x] 注册 `get_order_task()` 和 `revise_order_draft_quantity(...)`，实际 MCP 断言无 session_id，读模式不建单，执行工具风险门槛保持。

## Task 2：历史与 API

Files: run_store.py、新 history_routes.py，main.py/service.py、API 测试。
- [x] 首先测试空列表、分页、稳定顺序、标题裁剪和重启读取；调用 `store.list_sessions(limit=20,offset=0)`，断言仅包含运行记录摘要。
- [x] 参数化 SQL 查询，每次多取一行确定 has_more；首轮输入作为标题；不返回模型历史或技术 token。
- [x] 注册 spec 路径，disabled 返回结构化 503。状态与系统提示词加入任务查询协议；真实 MCP/HTTP 跨轮修改测试保留原审批行为。

## Task 3：历史入口

Files: web/src/history.ts、components/SessionHistory.tsx、App.tsx/CSS、tests。
- [x] 写列表解析、分页和加载失败测试；例如 `parseSessionPage({items:[],has_more:false})` 返回空列表，非法状态拒绝。
- [x] 列表显示标题、日期、状态；分页只读。切换复用 `loadSessionState(id)`；清空旧展示，捕获目标 ID 和代次。
- [x] 当前流禁止切换；审批/中断可切换并再找回；旧请求的成功和失败均不污染新会话。
- [x] 前端测试、生产构建与浏览器证据；API 与 UI 独立按固定契约实施。

## Task 4：验收

- [x] 全仓 Python/Ruff、前端 test/build、独立审查。
- [x] 更新 README、工具卡、路线、任务与验收报告，分增量提交。

## 执行记录

2026-10-11：沿用户已授权路线实施。领域与 MCP 独立实现，API 与前端按固定契约集成。真实 MCP/脚本客户端验证重建宿主后的第二行数量修改，浏览器验证历史找回与审批不执行。旧提示恢复和 UTC 时间问题均先失败后修复。最终全仓 513 passed / 21 skipped / 47 deselected，前端 73 passed，类型检查、生产构建、Ruff 和差异检查通过，独立复审无阻塞问题。验收见 reports/acceptance/2026-10-11-task-history.md。
