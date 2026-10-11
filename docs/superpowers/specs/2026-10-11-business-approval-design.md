# B3：取消与库存调整的业务审批

2026-10-11。承接已确认路线及用户「继续下一步开发」，直接实施本阶段。

## 目标与选择

沿用 B2 的可信会话、持久化业务快照、固定引用审批及事务复核模式；新增取消与库存调整操作预览。采用独立不可变操作引用，避免把业务逻辑放进 agent_core 或改变通用审批协议。与把当前数据仅拼成展示文本相比，这能保证用户批准的影响就是执行依据；与重写通用图节点相比，变更集中在 ERP 组合层。

Python >=3.12，不新增第三方依赖；保持 agent_core 零业务依赖。本机可信单用户范围，不增加支付/退款能力，不部署。原有评测、安全未提交改动保留。

## 契约

- scoped MCP 新增 prepare_cancel_order(order_id)、prepare_stock_adjustment(sku,delta)。保存预览不改订单、不动库存；负库存、不可取消状态提前返回业务错误。
- scoped cancel_order(operation_id,client_token?)、adjust_stock(operation_id,client_token?) 只接收已保存引用；原始同名工具只在 CLI/独立 MCP 保留。引用由可信会话绑定，schema 无 session_id。
- 操作有效 30 分钟，每次重新预览生成新 operation_id；无需可编辑版本。批准/拒绝仍只回填 pending_id 和决定；拒绝不执行。
- JSON：{operation_id,kind,status,expires_at,summary,invalid_reason}。kind 为 cancel_order/adjust_stock；status 为 open/changed/expired/executed。summary 是不可变业务依据；invalid_reason 是实时复核的可读原因或 null。
- 取消 summary：{order_id,customer,status_before,status_after,total_amount,note,items:[{sku,name,quantity,unit_price,line_total,stock_before,stock_after,warehouse}]}。金额来自原订单，不宣称执行退款。库存行不存在时 before/after/warehouse 为 null，明确该行无法回补。
- 调整 summary：{sku,name,delta,quantity_before,quantity_after,warehouse,stock_exists}。库存不存在时预测创建主仓记录；调整不改变商品售价。
- 事务内复核订单状态/客户/金额/明细与受影响库存，或商品名称及库存/仓库/记录存在性。变化返回 operation_changed，过期 operation_expired；须重新预览并审批。不静默刷新批准对象。
- 操作结果、业务变更、token 同事务提交。同引用同/新 token 只执行一次，返回原始结果。恢复核对先检查会话与操作类型，不能泄露其他会话结果。
- GET /api/sessions/{sid}/operations/{id} 返回原快照与实时有效性；错误保持 {error:{code,message,hint}}。会话 pending 增加可选 operation；不改变 checkpoint arguments。
- /api/runtime 增加 business_approvals_enabled。网页展示业务摘要；未核对、失效、类型/引用不匹配时禁用批准，拒绝可用。断线后仍展示同一批准对象。已执行结果提供业务成功/失败/拒绝卡片与原因。

## 验收

临时 SQLite、脚本模型验证预览无写入、拒绝、执行、库存/订单变动、过期、重复与并发、跨会话、不同操作类型、旧 token 冲突、重建宿主与响应丢失；真实 MCP/HTTP 路径和前端测试/构建/实际浏览器验证。不调用付费模型，不声称新增 PostgreSQL 硬崩溃矩阵已验收。
