# B3 Business Approval Implementation Plan

> **For agentic workers:** Use executing-plans for backend; dispatching-parallel-agents for independent frontend against the fixed contract. Track steps with checkboxes.

**Goal:** 取消与库存调整按明确业务影响审批，状态变化后要求重新确认。

**Architecture:** SQLite 保存操作快照，复用同事务变更方法；会话 MCP 替换两个原始写工具；API 恢复摘要与有效性；React 展示固定操作及失效提示。

**Tech Stack:** SQLAlchemy / FastMCP / FastAPI / LangGraph / React / TypeScript。

**Spec:** docs/superpowers/specs/2026-10-11-business-approval-design.md

## Global Constraints

Python >=3.12，不新增第三方依赖；保持 agent_core 零业务依赖。本机可信单用户，不部署、不实现支付或退款。模型无 session_id。只提交本阶段文件。

## Review Focus

- 新 token 重复同引用，不能二次回补/调整。
- 允许取消的订单状态从待付款变待发货也使审批失效。
- 缺失库存取消时明确不会回补，不伪造零库存。
- 新/旧同名工具恢复请求不能误核对旧请求；外会话 token 不泄露结果。
- 无摘要/失效引用只能拒绝；异步旧会话响应不能回填新会话。

## Task 1：领域快照与事务

Files: erp_store/db.py、mutations.py；新增 operations.py、tests/test_operations.py。
Interfaces: ErpOperations(engine,clock=None).prepare_cancel(session_id,order_id)、prepare_adjust(session_id,sku,delta)、get(session_id,operation_id)、execute(session_id,operation_id,kind,client_token)、lookup_operation(session_id,kind,arguments,token)。summary JSON 见 spec。

- [ ] 写失败测试并确认缺少实现；准备取消、调整后订单/库存未变。
```python
preview = operations.prepare_adjust('s1','A',-3)
assert preview['summary']['quantity_after'] == 97
assert repo.get_stock('A').quantity == 100
```
- [ ] 提取 cancel_order/adjust_stock 的事务内方法，原入口和旧 token request 保持不变。
- [ ] 实现不可变操作表、30 分钟时钟、实时有效性及锁内比较后执行。
- [ ] 领域回归覆盖状态/库存/仓库变更、缺失库存、过期、类型/会话隔离、新 token 重放、并发与取消后原结果。

## Task 2：MCP 与 API

Files: 新 operation_tools.py；修改 server.py、bridge.py、session_tools.py、API service.py/main.py；新增 operation_routes.py、MCP/API integration tests。
Interfaces: SessionErpTools.operations；runtime business_approvals_enabled；pending.operation；GET /api/sessions/{sid}/operations/{id}。

- [ ] 失败测试 scoped 工具 schema 不含 order_id/sku 于执行端，而包含 operation_id；独立 MCP 保持旧接口。
- [ ] 新预览工具注册及同名执行工具替换，恢复核对使用同一库/会话/类型。
- [ ] API 精确摘要与当前有效性读出，不改 checkpoint 参数；编写实际 MCP、HTTP、恢复和响应丢失回归。
```python
state = await service.session_state('s1')
assert state['pending_approvals'][0]['operation']['operation_id'] == preview['operation_id']
```

## Task 3：前端独立实现

Files: apps/web/src 与 tests only。新增 operations.ts 与 BusinessApprovalSummary.tsx，修改 App/AssistantMessage/protocol/session/result cards。

- [ ] 客户、原单金额、取消前后状态、回补明细；调整商品/仓库/数量前后/增减量。
- [ ] 当前引用从 API 核对；状态 changed/expired/executed 或缺失/类型错禁用批准，拒绝保留。
- [ ] 恢复/刷新/工具完成时重新核对，防异步串会话；操作引用不呈现给普通用户，技术详情可展开。
- [ ] 前端确定性测试、类型检查、构建；实际浏览器核对摘要、失效、拒绝与结果卡。

## Task 4：验收与提交

- [ ] 全仓 Python / Ruff、前端 test/build，独立审查并修复。
- [ ] 更新工具卡、业务路径、README、验收报告与任务阶段；保留已有未提交改动。
- [ ] 分域提交已验证增量，不推送、不部署。

## 执行记录

2026-10-11：用户明确继续下一步开发，沿已授权路线执行；已评估本阶段接口与事务边界，前端按固定 JSON 契约独立实施。
