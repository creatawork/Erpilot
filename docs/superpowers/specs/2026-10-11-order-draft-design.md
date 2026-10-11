# B2：订单草稿、版本与审批绑定

2026-10-11。用户要求继续已确认路线的下一步，本轮直接实施 B2。

## 范围和选择

草稿和成交结果均在现有 ERP SQLite 中保存；利用单个写事务复核和提交，避免业务库与图状态之间引入额外提交窗口。版本不可变，每次修改新增版本；旧版本保留以便恢复原审批明细。复用 LangGraph 的 pending_id、参数指纹、审批超时和恢复机制，不新建审批引擎。

前端增加可编辑的订单草稿和建单审批摘要。B3 的取消、库存调整审批摘要仍为后续阶段；B4 的自然语言省略指代、长期任务摘要和历史会话导航仍为后续阶段。

## 业务不变量

- Python >=3.12，不新增第三方依赖；保持 agent_core 零业务依赖。
- 草稿归属可信宿主提供的 session_id；该值不在模型工具 schema 中。跨会话获取、修改、提交和恢复核对不能泄露草稿或订单。
- `draft_id` 不变，revision 从 1 递增；修改需 expected_revision，旧版本修改返回 draft_changed；已提交草稿不得修改。
- 创建和修改仅保存草稿，不扣库存、不建单；同 SKU 合并，计价复用 B1。
- 默认报价有效期 30 分钟，UTC 时钟可注入；修改/刷新产生新版本并刷新有效期。
- 审批工具 `submit_order_draft(draft_id, revision, client_token?)` 只接受版本引用。当前网页 MCP 会话不再暴露原始 create_order，防止绕过草稿；旧 CLI/独立 MCP 工具兼容保留。
- 执行时在 BEGIN IMMEDIATE 事务内检查归属、最新版本、有效期、商品存在和在售状态、原始现价、折后单价、库存与批准快照完全一致。变化返回 draft_changed / quote_changed / stock_changed / draft_expired，提示修改/刷新后重新审批；不得静默改价或替代商品。
- 同草稿版本，即使新 token 再次提交也只对应一张订单。订单、库存扣减、幂等结果和草稿 order_id 在同一事务提交。
- 同 token 同请求重放返回原结果；同 token 不同请求返回 idempotency_conflict。已提交结果即使有效期过后仍可核对；新 token 重放也返回同一订单。
- 拒绝审批不调用提交工具，草稿保留；旧审批在草稿修改后不能执行，前端禁用旧版本批准并允许拒绝旧请求。

## 接口契约

草稿 JSON：`{draft_id, revision, customer, note, items, total_amount, expires_at, status, order_id}`。status 为 open / expired / superseded / submitted；items 为 `{sku, name, quantity, base_price, discount, unit_price, line_total, stock_before, stock_after}`。

- `GET /api/sessions/{session_id}/order-drafts` → `{items: OrderDraft[]}`，最新版本优先，最多 20 个草稿。
- `POST /api/sessions/{session_id}/order-drafts` body `{customer, items: [{sku,quantity}], note?}` → 草稿，HTTP 201。
- `PATCH /api/sessions/{session_id}/order-drafts/{draft_id}` body `{expected_revision,customer,items,note?}` → 新版草稿。
- `GET /api/sessions/{session_id}/order-drafts/{draft_id}?revision=N` → 指定版本；无 revision 时为最新版本。
- 错误 `{error:{code,message,hint}}`：not_found 为 404；draft_changed/quote_changed/stock_changed/draft_submitted 为 409；draft_expired 为 410；其他参数或领域错误为 422；无草稿运行环境为 503。
- `/api/runtime` 增加 `drafts_enabled: boolean`，旧客户端可忽略；会话快照待审批项增加可选 `draft`，原参数不改。
- MCP 增加 prepare_order_draft / get_order_draft / revise_order_draft（三个无需审批的草稿工具）；submit_order_draft 是 single_confirm。所有工具参数均无 session_id。

## 宿主和恢复

SessionErpTools 启动时发现工具 schema，按 session_id 生成 handler，配置相同数据库的草稿服务与核对器。ChatService 允许注入会话工具工厂和核对器工厂，构建每个会话的运行时时使用；核心运行时不识别 ERP 草稿。

首次批准前的核对器返回 absent 后才执行；提交后响应丢失则核对器读同一草稿版本的已提交结果。草稿版本、实际订单和幂等记录从 ERP 库恢复，checkpoint 只记录批准的版本引用。

## 验收

固定临时库和脚本化模型覆盖：多商品明细、数量修改跨折扣档、同 SKU 合并、乐观版本冲突、跨会话隔离、价格/库存/商品状态变化、过期、重复 token 与新 token 重放、并发只建一单、拒绝、旧版本批准、断连重建宿主后批准、真实 MCP 和 HTTP 路径。前端协议测试、类型检查、构建与浏览器操作验证草稿编辑及审批摘要。真实模型表现另行评测，本轮不消耗聊天模型 token。
