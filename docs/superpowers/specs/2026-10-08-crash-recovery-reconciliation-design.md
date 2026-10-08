# 崩溃恢复与对账设计（提案）

- 日期：2026-10-08
- 状态：硬崩溃矩阵已在隔离 PostgreSQL 上通过；ADR-0010 仍为提案、待评审。见 [验收报告](../../../reports/acceptance/2026-10-08-crash-recovery.md)
- 范围决策：[ADR-0010](../../adr/0010-crash-recovery-reconciliation-scope.md)
- 实施计划：[崩溃恢复与对账实施计划](../plans/2026-10-08-crash-recovery-reconciliation.md)
- 基线架构：[ADR-0009](../../adr/0009-langgraph-runtime.md)

## 1. 目标与边界

让本机单 API 进程在页面刷新、SSE 断开或进程硬重启后，安全找回待审批调用、识别业务提交事实，并继续尚未完成的回答。相同获批调用最多提交一次；无法确认提交结果时必须明确报告待核对，不能换 token 或盲目重做。

本设计覆盖旧恢复协议 R01–R10 及 ERP 的四种写工具。继续使用 LangGraph graph checkpoint（PostgreSQL）、业务库及 `MutationRequestRow`（SQLite）、API `RunStore`（SQLite 展示投影）。本设计不做代码实现、多 worker/分布式锁、身份认证、自动补偿、跨库事务或真实模型评测门槛。

## 2. 不变量

1. 写工具只能在代码级审批后执行；checkpoint 保存的工具名、规范化参数、参数指纹、工具 schema 版本和 `client_token` 在批准后不可替换。
2. token 在首次审批展示前已生成并 checkpoint；恢复、超时和工具级安全重试均复用该 token。token 和原请求不匹配时终止恢复，不生成新 token。
3. `MutationRequestRow` 是写入成功/结果的唯一事实。RunStore、trace、SSE 和模型答复都不是业务提交证明。
4. 未知业务状态时停止写动作；系统不能把超时、连接断开或缺少展示记录解释为“没有执行”。
5. 已完成的写工具结果只可回填，不可重放为新业务动作。回答可以重生成，但不能重新规划/执行已经完成的写调用。
6. API 单进程会话锁同时保护恢复、审批继续和取消竞争；连接断开不会释放业务执行权，也不会自动取消 graph。
7. graph schema 不兼容、工具 schema 变化或参数指纹不同均 fail closed，原 checkpoint 和诊断证据保留。

## 3. 持久化与组件契约

### 3.1 Graph checkpoint

扩展现有可序列化 `pending_calls` / `tool_results` 状态。每个风险调用至少保留：

```text
call_id, tool_name, tool_schema_version, normalized_arguments,
arguments_fingerprint, client_token, pending_id,
approval_status, approval_created_at_utc, approval_expires_at_utc,
invocation_status, structured_result, reconciliation_error
```

`approval_status` 取 `pending | approved | denied | expired | cancelled`；`invocation_status` 取 `prepared | waiting_approval | approved | executing | succeeded | failed | unknown | denied | expired | cancelled`。Graph 中只写 JSON/MsgPack 可序列化的普通值。已有消息格式和公开 `AgentEvent` 名称保持兼容。

准备阶段生成期限和 token，然后结束节点并 checkpoint；只有此 checkpoint 成功后，API 才能向客户端发出 `approval_pending`。批准决定进入同一 graph state，并由 checkpoint 持久化。批准被持久化后，pending TTL 不再撤销该批准。

### 3.2 业务幂等查询

在 `erp_store` 增加只读、公开的 token 查询接口，并复用现有四个写操作的 canonical request 构造逻辑。结果只有三种：

- `found(result)`: token 存在且规范化请求完全一致，回填记录的首次成功结果。
- `absent`: 查询成功且没有 token 记录；仅当 checkpoint 中仍有有效批准、参数和工具版本匹配时，允许调用原 handler 一次并传入原 token。
- `conflict`: token 存在但绑定另一请求；转为 `unknown`/冲突终态并禁止执行。

查询数据库失败是 `unknown`，不等于 `absent`。该路径不调用写 handler，保留原 token 和 checkpoint，返回明确的“结果待核对”。后续显式恢复先重新查询；只有明确 `absent` 才允许继续。工具执行收到结构化业务错误时按失败结果保存，不将其误判成已提交成功。

API 组合层通过 MCP bridge 提供 `MutationReconciler`，不让 `agent_core` 依赖 `erp_store`。该适配器按相同 `db_path` 查询业务库；测试可注入确定性替身。若 PostgreSQL checkpoint 无法写入，API 不得先执行新写副作用。

### 3.3 RunStore 展示投影

RunStore 不新增 execution owner、invocation、approval 权威记录。扩展当前 `presentation_event` 读取能力：每个 run 的 `seq` 单调递增，数据库唯一约束 `(run_id, seq)`，事件写入和 seq 分配在同一 SQLite 事务中。保留现有 event 内容与去重字段。现有 30 天清理仅可删除终态 run 的投影事件；活动、待审批、获批未收口及 `unknown` run 的事件不能被清理，以保证游标重放。历史库添加唯一约束前先查重；发现冲突就保留原数据并拒绝升级，不能自动丢弃或重排事件。展示投影缺失不能阻止从 checkpoint 恢复，也不能替代 token 查询。

## 4. 审批、对账和取消流程

### 待审批

审批准备节点在 UTC 时钟下设置 `expires_at = created_at + ERPILOT_APPROVAL_TTL_SECONDS`（默认 1800 秒），写入 checkpoint 后再展示。刷新、重连和重启读取原期限，不重新计算。客户端在 `expires_at` 后提交批准/拒绝时返回 `410 approval_expired`；恢复器把 pending 记录确认为 `expired`，不得执行 handler。用户重新发起时创建新的调用、token 和审批。

### 批准及写执行恢复

批准时先确认 pending_id、会话、工具 schema 版本和参数指纹匹配，并把批准写入 checkpoint。写节点在每次运行前都按 token 调用 `MutationReconciler`：

1. `found`：写回首次结果和 `succeeded`，不调用 handler。
2. `absent`：使用绑定的原参数和原 token 执行 handler；结果与状态写回 graph checkpoint。
3. `conflict`：不调用 handler，状态置 `unknown`，展示参数冲突和关联 token。
4. 查询异常/超时：不调用 handler，状态置 `unknown`，展示“结果待核对”；下次显式恢复只能从查询开始。

若业务提交已成功但 checkpoint 尚未记录结果，服务重启后会在写节点再次先查 token，从 `found` 取回结果。若业务事务尚未提交且数据库可查询为 `absent`，以原 token 运行一次；唯一约束和现有事务幂等继续作为最终防线。R04/R05 的断点都必须覆盖这一行为。

### 并发恢复与取消

一个会话的 chat、approve/resume、retry 和 cancel 都使用 `ChatService` 同一 session lock。恢复拿锁后再次读取 checkpoint，避免以过期快照执行。竞争中的第二个请求返回 `409 session_busy` 和当前状态，不进行状态修改。

新增显式取消入口。若调用仍在 pending，取消会持久化 `cancelled`，之后迟到批准不能改变状态。若恢复已在执行，取消不强杀工具 handler；等待锁后先按 token 对账，并只停止尚未开始的调用。已提交结果回填；查询不可用则保留 `unknown`。此语义不承诺回滚已发生业务副作用。

## 5. HTTP 与事件恢复契约

保留现有接口：

- `GET /api/sessions/{session_id}/state`
- `POST /api/chat/approve/stream`
- `POST /api/sessions/{session_id}/resume/stream`
- `POST /api/chat/approve` 的 `{ok: boolean}` 兼容语义

状态快照向后兼容地增加 `run_id`、整体状态、逐调用状态、`pending_approvals`（含 `pending_id`、参数摘要、`expires_at`）、结构化 `tool_results`、`unknown` 原因和 `last_seq`。

新增接口：

| 接口 | 成功语义 | 关键错误 |
|---|---|---|
| `POST /api/sessions/{session_id}/cancel` | 以当前 checkpoint 为准，幂等停止尚未开始的动作；对已开始的写调用对账 | `404` 未知 session；`409` 正在执行；`unknown` 时不宣称取消了业务效果 |
| `GET /api/runs/{run_id}/events?after_seq=N` | 先补发所有 `seq > N` 的持久化事件，再跟随实时事件；客户端以 `(run_id, seq)` 去重 | `404` 未知 run；无效游标 `422` |

审批决策错误：未知 pending 为 `404`，相反/过期版本为 `409`，审批过期为 `410`，不支持 checkpoint/schema 为 `409 checkpoint_incompatible`。旧 `{ok: boolean}` 路径保留其响应结构，不通过它执行重启后的 token 对账。

客户端启动顺序：先获取会话快照并把消息区重置为快照；随后以快照 `last_seq` 连接事件 SSE。服务端从持久化事件补发序号大于游标的内容，然后继续推送实时事件；两者交界出现重复时由客户端去重。多个 pending 以 `pending_id` 和 `call_id` 独立关联。展示最终 answer 时以快照替换，不把恢复前后 delta 重复拼接。

## 6. R01–R10 验收映射

| ID | 注入点与必须证明的结果 |
|---|---|
| R01 | token/参数/审批 checkpoint 成功后、事件展示前硬终止；重启后是同一 pending_id 和原期限，业务零变化。 |
| R02 | 待审批刷新、SSE 断开和 API 硬重启；期限不延长。到期后批准返回 410，handler 调用次数为零。 |
| R03 | 批准 checkpoint 后、写节点前硬终止；重启使用原 token/参数执行一次，拒绝分支业务零变化。 |
| R04 | 写入事务提交前硬终止；恢复查询结果 `absent`，原 token 重试后最多一次业务变化、无半笔记录。 |
| R05 | 业务事务提交后、handler 响应/checkpoint 结果前硬终止；恢复查询 `found` 并回填原结果，业务表不二次变化。 |
| R06 | 工具结果 checkpoint 后、最终回答前硬终止；恢复只重生成回答，不再次调用已完成写工具。 |
| R07 | 同一 pending 的 approve/deny 并发、重复和相反决定；只存在一个持久化决定，迟到操作不执行工具。 |
| R08 | 同 session 的 resume/cancel 并发；单进程最多一个执行者，取消不伪装为业务回滚，已开始调用先对账。 |
| R09 | 业务查询异常、同 token 异参、checkpoint/schema/tool 版本不兼容；均 fail closed，保留 unknown/诊断，不发新 token。 |
| R10 | 快照与订阅并发交界、重复事件、两个待审批调用；无事件缺失、重复审批卡或错配 tool result。 |

R03–R06 对四种写工具分别覆盖。最少要有每种工具的事务前/后快照和 mutation 执行计数。硬终止由独立 worker 子进程完成，父进程检查 PostgreSQL checkpoint 与临时 ERP SQLite；所有破坏性试验使用隔离库。真实模型行为评测与本验收分开记录，不计入 R01–R10 通过率。

## 7. 完成标准与风险

- R01–R10 全部通过；R03–R06 四工具覆盖完整；三项零容忍问题为零。
- API/前端刷新、恢复、过期和 `unknown` 展示符合本说明；事件游标可重放、去重。
- 运行、审批与业务状态之间的证据关联完整：revision、case、session/run/pending/token、注入点、业务表前后值、handler 次数、checkpoint/trace 路径及失败原因。
- 回归至少覆盖现有 API、graph approval/persistence、mutation 幂等和 web protocol/session UI 测试；PostgreSQL checkpoint 集成使用独立 schema，ERP 写入使用临时库。
- 单进程限制是明确约束。部署到多个 API worker 前必须新增分布式 execution lease 设计，不能把 `asyncio.Lock` 当成跨进程互斥。

## 8. 当前仓库到目标的差距

当前已有：稳定写 token 在 interrupt 前准备、PostgreSQL graph checkpoint、审批后续跑 SSE、单进程 session lock、SQLite mutation token 幂等表、RunStore 展示事件投影。仍需：mutation 只读按 token 查询 API、checkpoint 中审批期限与生命周期、graph 写节点前置对账、`unknown` 可观察状态、cancel 与 resume 共用互斥语义、事件游标补发端点、前端过期/待核对展示、子进程硬崩溃矩阵以及四写工具覆盖。不能把已有刷新/待审批重启演示计为 R01–R10 已通过。
