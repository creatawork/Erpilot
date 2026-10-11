# B2 Order Draft Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for the backend; dispatching-parallel-agents for the frontend against the frozen API contract. Track steps with checkboxes.

**Goal:** 将确定性报价明细保存为会话草稿，审批绑定版本并在事务内复核后建单。

**Architecture:** ERP SQLite 保存不可变草稿版本；提交复用建单事务内部方法。API 组合层注入会话工具与核对器，LangGraph 的审批协议保持原状。前端依据后端草稿展示金额和库存影响。

**Tech Stack:** Python 3.12 / SQLAlchemy / FastMCP / FastAPI / LangGraph；React / TypeScript / 现有 Node 测试。

**Spec:** `docs/superpowers/specs/2026-10-11-order-draft-design.md`

## Global Constraints

- Python >=3.12，不新增第三方依赖；保持 agent_core 零业务依赖。
- 不修改已有历史订单和幂等记录；不合并工作区中既有未提交评测/安全改动。
- 模型参数不承载可信 session_id；审批只提交决定，不接受改写草稿引用。

## Review Focus

- 新 token 重复提交同草稿：只建一单且仅扣一次库存。
- 旧批准版本在用户修改后不能成交，即使原商品价格未变。
- 价格变化后再次变回仍以执行时条件复核；库存增减即使足量也需要重新确认。
- 恢复核对器不能用其他会话的 draft_id 或 client_token 返回订单。
- 草稿修改接口在审批挂起时可使用，但不能改变 checkpoint 内旧批准参数。

## Task 1：草稿领域与原子提交

**Files:** 新建 `packages/erp_store/src/erp_store/drafts.py`、`packages/erp_store/tests/test_drafts.py`；修改 `db.py`、`mutations.py`。

**Interfaces:** `ErpDrafts(engine, clock=None)` 提供 `prepare(session_id,customer,items,note=None)`, `get(session_id,draft_id,revision=None)`, `list_latest(session_id)`, `revise(session_id,draft_id,expected_revision,customer,items,note=None)`, `submit(session_id,draft_id,revision,client_token)` 与 `lookup_submission(session_id,arguments,token)`。结果为 Spec 中 JSON；submit 返回 Order。

- [x] 写固定库回归并先确认失败：prepare 不动库存，revise 从 9 改 10 件产生新版本、980 元。
```python
draft = drafts.prepare("s1", "客户", [("A", 9)])
revised = drafts.revise("s1", draft["draft_id"], 1, "客户", [("A", 10)])
assert revised["revision"] == 2 and revised["total_amount"] == 980.0
```
- [x] 把现有建单函数的事务内部分提取为 `_create_order_in_session`，原入口保持契约。
- [x] 实现不可变版本行；修改检查当前版本，提交检查条件和有效期；同事务写入草稿提交状态、订单、库存和 mutation token。
- [x] 测试过期、跨会话、旧版本、涨价、库存变动、下架、同/新 token 重放、并发提交；通过 erp_store 全部测试。

## Task 2：会话 MCP、宿主与 HTTP

**Files:** 新建 `packages/mcp_erp/src/mcp_erp/draft_tools.py`、`session_tools.py`、`packages/mcp_erp/tests/test_draft_tools.py`、`apps/api/tests/test_order_drafts.py`；修改 server.py、bridge.py、service.py、main.py；草稿 HTTP 路由独立为 `apps/api/src/erpilot_api/draft_routes.py`。

**Interfaces:** SessionErpTools 提供 `.tools` 启动模板、`.for_session(session_id)`、`.reconciler(session_id)`、`.drafts`。API 工厂注入 `session_tools_factory`, `session_reconciler_factory`, `order_drafts`。

- [x] 先用 MCP 和 HTTP 回归证明无可信会话参数、跨会话不可读取、原始 create_order 在会话面不可发现。
- [x] 会话 MCP 按 Spec 注册四个工具；提交风险 single_confirm，模板 handler 按会话绑定。
- [x] ChatService 所有运行时入口使用会话工具/核对器；HTTP 草稿 CRUD 与待审批摘要复用同一草稿服务。
- [x] 脚本化模型申请 submit 后断连，重建服务再批准；改单后批准旧请求没有订单；拒绝没有订单；响应丢失可用核对器读原订单。

## Task 3：草稿编辑与审批摘要（独立前端工作）

**Files:** 仅 `apps/web/src` 和 `apps/web/tests`；新组件 OrderDraftPanel；请求与类型独立 drafts.ts；按 Spec 对接既有 App。

- [x] 从 runtime 的 drafts_enabled 判断可用性，查询会话草稿；会话切换/恢复/工具完成后刷新，防旧请求回填新会话。
- [x] 支持客户、SKU、数量和多行编辑；保存 PATCH 指定 expected_revision，成功刷新确定性价格明细。
- [x] submit_order_draft 审批显示草稿明细和金额，版本落后时禁用批准但允许拒绝。
- [x] 草稿提交按钮通过既有聊天路径请求该版本的 submit；存在旧挂起请求时先结束旧审批再提交。
- [x] Node 协议测试、类型检查、构建；在实际浏览器验证编辑、保存、新版本审批和拒绝。

## Task 4：验收与路线收口

- [x] 全仓 pytest / ruff、前端测试与构建，保留跳过与未运行范围。
- [x] 独立审查后修复明确问题，回归验证。
- [x] 更新工具卡、README、业务路线和验收报告；仅提交 B2 文件及任务标题对应变更。

## 执行记录

2026-10-11：承接用户「继续下一步」，沿当前 feature 分支执行，不重复询问启动授权。前端按上述固定 HTTP/JSON 契约可与后端独立开发；同一文件只由一个执行者修改。基线为 dc5bbbd，既有评测/安全未提交改动保留。

2026-10-11：B2 已实现并通过全仓回归（447 passed / 21 skipped / 47 deselected）、前端 60 测试与构建；独立审查两处页面问题已修复并复核。实际浏览器验证通过，报告见 reports/acceptance/2026-10-11-order-draft.md。未运行付费模型评测或新增 PostgreSQL 硬崩溃矩阵。
