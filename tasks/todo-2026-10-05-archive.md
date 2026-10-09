> 历史存档：2026-10-07 从 tasks/todo.md 原样保留。此清单不是当前执行入口；状态按原日期保留，不代表 ADR-0008 已接受或 R01–R10 已完成。当前任务见 [todo.md](todo.md)。

# 下一阶段任务清单

日期：2026-10-05。配套 [总体规划](plan.md)。全部为未来工作，本次未开展代码开发。

状态统一：未开始 → 进行中 → 待验收 → 完成；仅验收证据齐全后勾选。建议角色不是已指派人员。估算为投入小时，不是日历交付承诺。下列“拟新增”路径尚不存在。

## T01：校准 edge-08 的行为与判分口径

- [x] T01 完成并附验收证据。（2026-10-05）

**优先级/建议负责人：** P0 / 评测；实际负责人待分配。

**依赖：** 无。

**范围与目的：** 先区分模型擅自替换筛选条件与检查器遗漏合法状态两类问题，固定可判别的正反例。

**预计投入：** 2–3 小时；涉及 4 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- docs/eval-annotation-guide.md
- packages/evals/src/evals/cases.py
- packages/evals/src/evals/checks.py
- packages/evals/tests/test_checks.py

**验收条件：**

- [x] 列出”明确说明状态不支持并请求澄清”的通过例与”擅自改查已退款”的失败例；仅提及合法枚举不能判通过。（标注标准 §5.1 正反例表 + test_checks.py 四个回归样例；归因：模型行为正确（过程消息澄清+按最近似合法状态披露查询），失败源于检查器遗漏合法枚举「已退款」且判据过弱）
- [x] 明确采用最终答复还是完整助手轨迹评分；若需扩充采集范围，先拆出依赖任务，不能只补一个枚举子串。（采用完整助手可见轨迹评分（§5 v2）；runner 本就逐步收集文本，无需扩充采集，仅改判分范围为过程消息+最终答复拼接）
- [x] 旧报告保持原样；新增口径有版本与变更理由，原失败不能回写为通过。（20261005-142553 未改动；§5.1 记录 v2 版本与变更理由，重跑归 T11）

**验证：** uv run pytest packages/evals/tests/test_checks.py packages/evals/tests/test_cases.py（全过）；uv run pytest 全仓 180 项离线单测全绿 + ruff 通过。人工核对正反例已并入 §5.1。真实模型定点诊断待 T11 统一执行。

**交付物：** 标注补充（§5/§5.1）、评分回归样例（test_checks.py 9 项）及 edge-08 归因记录（§5.1）。

## T02：定义真实写错误自愈观察集

- [x] T02 完成并附验收证据。（2026-10-05）

**优先级/建议负责人：** P1 / 评测；实际负责人待分配。

**依赖：** T01。

**范围与目的：** 补齐 M4 未完成的 insufficient_stock 与 invalid_transition 观察口径，为后续真实模型运行准备隔离样本。

**预计投入：** 2–3 小时；涉及 4 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- packages/evals/src/evals/write_cases.py
- packages/evals/tests/test_cases.py
- docs/eval-annotation-guide.md
- docs/error-recovery-log.md

**验收条件：**

- [x] 两类错误各至少一个固定 seed 场景，记录允许的只读复核、禁止的擅自替换操作及期望业务状态。（adv-24 零库存出库 → insufficient_stock、adv-25 在售商品重复上架 → invalid_transition；场景口径表与预注册场景（取消已签收订单需真人放行）见标注标准 §7.2；test_write_error_scenarios_trigger_on_seed 自动验证两类错误在种子库真实可触发且 hint 非空）
- [x] 拒绝或业务失败必须零业务变更；若模型提议修改数量或上架商品，必须新请求及新审批，不沿用原批准。（两条 case 均带 StateExpectation(unchanged)，test_write_error_cases_expect_zero_business_change 钉住；"新写操作须新审批"写入 case 考点与 §7.2）
- [x] 清楚区分离线 fixture、脚本化模型、真实模型与真人审批证据；真实模型未跑前不填成功数字。（§7.2 证据类别表四类分开；error-recovery-log.md 写路径观察记录模板落档，预注册场景执行前不填结果）

**验证：** uv run pytest packages/evals/tests/test_cases.py（12 项全过，含触发验证）；全仓 187 项离线单测 + ruff 通过。种子库触发为自动核验（非仅人工）。真实执行归 T11。

**交付物：** 可交给 runner 的写错误样例（WRITE_ERROR_CASES，需配 ScriptedPolicyGate + state_engine 运行，suite 接线范围冻结归 T11）和观察记录模板（error-recovery-log.md）。

## T03：冻结恢复契约与 ADR 草案

- [ ] T03 完成并附验收证据。（内容交付 2026-10-05：ADR-0008 草案 + 恢复协议明细 + 桌面推演；**待项目负责人评审**，评审接受后勾选）

**优先级/建议负责人：** P0 / 后端/规划；实际负责人待分配。

**依赖：** 无；吸收 T01/T02 结论后通过 C1。

**范围与目的：** 把总体规划第 4 节转成一致的数据、状态与 API 契约，先解决恢复边界再开发存储。

**预计投入：** 3–4 小时；涉及 3 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- docs/adr/0008-persistent-run-recovery.md（已建）
- docs/plans/恢复协议明细.md（已建）
- tasks/plan.md（§4 已加冻结指向）

**验收条件：**

- [x] 定义 session/run/invocation/approval/event 字段、唯一约束、检查点、并行调用组和合法状态迁移；列出落盘与业务提交各窗口。（恢复协议明细 §2/§3：五表字段与唯一约束——活动 run 部分唯一索引、token 唯一、决策条件更新；W1–W6 窗口表）
- [x] 确定 TTL、未知结果处理、工具参数/版本校验、事件保留与旧 API 兼容策略；区分订阅断开和显式取消。（明细 §2（TTL 30 分钟可配、UTC、注入时钟；首版不清理）、§3（unknown 三分支）、§4（旧入口兼容 + 新端点错误码 404/409/410；断开≠取消））
- [x] 记录存储位置、无损升级、单进程约束、保留策略与验收阈值；技术负责人评审后方可标 ADR 已接受。（明细 §2/§6 + ADR-0008；ADR 状态保持"草拟"，验收阈值已冻结：R01–R10 全过、35 条视图 ≥34、零容忍三项）

**验证：** 按 R01–R10 逐场景桌面推演（明细 §7：多调用一半完成、同意/拒绝竞争、查不到 token 三路径均闭合，未发现需修订规划的安全恢复路径）。

**交付物：** 已成文恢复协议（ADR-0008 草案 + 明细冻结稿）；评审通过后标 ADR 已接受并进入批次 B。

### 检查点 C1：验收与契约

- [ ] T01–T03 已完成；评分正反例、状态/API/数据契约经过核对；技术决策有负责人确认。（2026-10-05：T01、T02 完成附证据；T03 交付 ADR-0008 草案与恢复协议明细，R01–R10 桌面推演闭合——**待项目负责人评审**）
- [ ] 项目负责人审阅证据后进入下一批次；问题回到对应任务，不能仅凭总通过率放行。

## T04：落盘可重启读取的运行快照

- [x] T04 完成并附验收证据。（2026-10-05：只读会话快照接入 API；`test_run_store.py` 与 `test_api.py::test_session_history_survives_service_reconstruction` 验证重建、工具组历史与唯一活动 run。写工具面待 T05 的审批/token 检查点后接入。）

**优先级/建议负责人：** P0 / 后端；实际负责人待分配。

**依赖：** T03、C1。

**范围与目的：** 先让一条只读会话的运行记录与已完成历史跨服务重建可查询，验证应用存储边界与升级方式。

**预计投入：** 2–4 小时；涉及 4 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- apps/api/src/erpilot_api/run_store.py（拟新增）
- apps/api/src/erpilot_api/service.py
- apps/api/tests/test_run_store.py（拟新增）
- apps/api/tests/test_api.py

**验收条件：**

- [x] 保存并重读 session/run、原用户输入、完整工具组检查点及最终结果，重建 ChatService 后仍可查。
- [x] 活动 run 的创建/占用使用持久化唯一约束或版本条件更新，重复请求不创建第二个活动执行。
- [x] 已有业务数据库无需 seed；schema 不兼容时停止恢复并保留数据，不覆盖原记录。

**验证：** uv run pytest apps/api/tests/test_run_store.py apps/api/tests/test_api.py；用临时文件库重建服务对象而非复用同一内存实例。

**交付物：** 运行快照存储与兼容性测试证据。

## T05：在审批前持久化写调用身份

- [ ] T05 完成并附验收证据。

**优先级/建议负责人：** P0 / 后端；实际负责人待分配。

**依赖：** T04。

**范围与目的：** 以 adjust_stock 为首条切片，把参数、token 和审批请求固化到运行记录，移除恢复对旧闭包对象的依赖。

**预计投入：** 3–4 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- apps/api/src/erpilot_api/run_store.py
- packages/agent_core/src/agent_core/approval.py
- packages/mcp_erp/src/mcp_erp/bridge.py
- packages/agent_core/tests/test_approval.py
- packages/mcp_erp/tests/test_mcp_erp.py

**验收条件：**

- [ ] 首次调用 MCP 写 handler 之前持久化服务端生成的 token、call_id、规范化参数与 pending_id；审批绑定原参数指纹。
- [ ] 重启后从数据重建待审批动作；无 gate 不提供写工具的不变量及 CLI/同步评测门行为仍成立。
- [ ] 同参数恢复使用原 token；异参数或不兼容工具版本不得继承原批准；未经审批不得直接调用恢复入口。

**验证：** uv run pytest packages/agent_core/tests/test_approval.py packages/mcp_erp/tests/test_mcp_erp.py；R01/R02 的待审批恢复场景。

**交付物：** 可跨重启识别的写意图；不以序列化 Future/闭包代替数据契约。

## T06：按 token 核对已提交业务结果

- [ ] T06 完成并附验收证据。

**优先级/建议负责人：** P0 / 后端；实际负责人待分配。

**依赖：** T05。

**范围与目的：** 打通 adjust_stock 在提交成功但响应丢失时的核对路径，从业务幂等记录修复运行状态。

**预计投入：** 3–5 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- packages/erp_store/src/erp_store/mutations.py
- packages/mcp_erp/src/mcp_erp/bridge.py
- apps/api/src/erpilot_api/service.py
- packages/erp_store/tests/test_write_reliability.py
- apps/api/tests/test_recovery.py（拟新增）

**验收条件：**

- [ ] 内部查询能区分成功结果、无记录、查询失败和参数冲突，不增加模型可见业务工具数量。
- [ ] 提交后响应丢失仍返回首次成功结果；只有满足契约时才按原 token 重试，无记录本身不能触发新 token 写入。
- [ ] 库存调整覆盖批准后/提交后重启，核对状态修复正确且库存仅变更一次；拒绝路径四张业务表不变。

**验证：** uv run pytest packages/erp_store/tests/test_write_reliability.py apps/api/tests/test_recovery.py；按 R03–R06 检查库存前后值与成功提交记录数。

**交付物：** 第一条业务恢复闭环及 C2 演示记录。

### 检查点 C2：首条恢复闭环

- [ ] T04–T06 定向测试通过，库存调整的待审批重启/提交后重启/拒绝零变更可复现。
- [ ] 项目负责人审阅证据后进入下一批次；问题回到对应任务，不能仅凭总通过率放行。

## T07：让审批决定与失效规则跨重启保持一致

- [ ] T07 完成并附验收证据。

**优先级/建议负责人：** P0 / 后端；实际负责人待分配。

**依赖：** T06。

**范围与目的：** 实现持久化审批决定、过期及竞争处理，避免重试提交、旧页面按钮和重启改变审批含义。

**预计投入：** 3–5 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- packages/agent_core/src/agent_core/approval.py
- apps/api/src/erpilot_api/run_store.py
- apps/api/src/erpilot_api/main.py
- packages/agent_core/tests/test_approval.py
- apps/api/tests/test_api.py

**验收条件：**

- [ ] 决定与状态条件更新原子落盘；相同重复决定返回原结果，相反决定冲突，已过期不执行。
- [ ] 请求校验 run/session/参数指纹和版本；错误归属、未知/已决/过期审批均有确定响应及兼容策略。
- [ ] 重启、刷新不延长 TTL；取消未决请求后迟到批准无效，已执行业务保留真实结果。

**验证：** uv run pytest packages/agent_core/tests/test_approval.py apps/api/tests/test_api.py；注入时钟验证 TTL，覆盖并发批准/拒绝与重启后的重复提交。

**交付物：** 持久化审批生命周期与 R02/R07/R08 证据。

## T08：提供任务快照及恢复事件流

- [ ] T08 完成并附验收证据。

**优先级/建议负责人：** P0 / 后端；实际负责人待分配。

**依赖：** T07。

**范围与目的：** 把持久化状态作为 API 恢复来源，分离 SSE 订阅生命周期与任务执行生命周期。

**预计投入：** 3–5 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- apps/api/src/erpilot_api/main.py
- apps/api/src/erpilot_api/service.py
- apps/api/src/erpilot_api/events.py
- apps/api/src/erpilot_api/run_store.py
- apps/api/tests/test_recovery.py

**验收条件：**

- [ ] 实现 T03 冻结的查询/事件/resume/cancel 契约，事件含 run_id 和单调 seq；快照与实时流交界无缺口。
- [ ] 断开订阅不等于取消写任务；并发 resume 只启动一个执行者，多待审批/部分已完成调用均可恢复。
- [ ] 已完成写调用只回填原结果再生成回答；模型历史合法，最终答复按 run 关联 trace，不使用全局最近 trace 代替。

**验证：** uv run pytest apps/api/tests/test_recovery.py apps/api/tests/test_api.py；覆盖 AnyIO 断连、重连游标、重复恢复、取消与已提交结果。

**交付物：** 可供前端接入的恢复 API 及协议样例。

## T09：让刷新后的页面找回真实任务状态

- [ ] T09 完成并附验收证据。

**优先级/建议负责人：** P0 / 前端；实际负责人待分配。

**依赖：** T08。

**范围与目的：** 基于服务器快照恢复对话与审批卡，明确呈现待审批、已执行未回答及结果未知的区别。

**预计投入：** 2–4 小时；涉及 4 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- apps/web/src/protocol.ts
- apps/web/src/App.tsx
- apps/web/src/App.css
- apps/web/tests/protocol.test.mjs

**验收条件：**

- [ ] 刷新/断网重连后按 session/run 找回任务，不重新提交原用户消息；事件去重，不重复拼接回答或新增同一审批卡。
- [ ] 批准/拒绝提交失败可重试，过期/已决/冲突有明确反馈；未知结果不显示成功，“业务已执行”不显示可撤销假象。
- [ ] 保留旧报价/批准/拒绝/错误恢复流程；浏览器验证刷新、离线恢复与旧卡点击，保留记录且无未处理错误。

**验证：** apps/web 下 npm test 与 npm run build；真实浏览器按 R02/R06/R07/R10 操作，核对后端快照与业务库。

**交付物：** 恢复交互及浏览器证据；C3 核查记录。

### 检查点 C3：恢复交互

- [ ] T07–T09 测试与前端构建通过；浏览器刷新、断连、过期/冲突和未知结果场景已核对。
- [ ] 项目负责人审阅证据后进入下一批次；问题回到对应任务，不能仅凭总通过率放行。

## T10：扩展四工具崩溃恢复验收

- [ ] T10 完成并附验收证据。

**优先级/建议负责人：** P1 / 后端/测试；实际负责人待分配。

**依赖：** T08；与 T09 可独立推进。

**范围与目的：** 把已验证的库存调整恢复机制扩展到建单、取消订单、上下架，建立独立进程故障测试。

**预计投入：** 3–5 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- apps/api/tests/test_recovery.py
- packages/erp_store/tests/test_write_reliability.py
- packages/mcp_erp/tests/test_mcp_erp.py
- scripts/demo.py
- apps/api/tests/test_demo.py

**验收条件：**

- [ ] 四工具覆盖 R03–R06 的关键提交窗口；建单不重复建单/扣库存，取消不重复回补，上下架重放返回首次结果。
- [ ] 独立子进程硬终止并重启，记录数据库状态与原 token；多审批/部分调用成功、并发恢复与异参冲突都有回归。
- [ ] 保留原四场景无密钥演示，并添加可复现恢复场景；所有场景独立临时库，不修改常用业务库。

**验证：** uv run pytest apps/api/tests/test_recovery.py packages/erp_store/tests/test_write_reliability.py packages/mcp_erp/tests/test_mcp_erp.py apps/api/tests/test_demo.py；uv run python scripts/demo.py。新增演示开关需实施者同步文档。

**交付物：** R01–R10 故障矩阵报告及恢复演示素材。

## T11：补齐真实模型与真人审批验收证据

- [ ] T11 完成并附验收证据。

**优先级/建议负责人：** P1 / 评测/项目负责人；实际负责人待分配。

**依赖：** T02、T09、T10。

**范围与目的：** 完成真实模型写错误观察、真人批准/拒绝与完整基线，分开报告工程恢复能力和模型行为。

**预计投入：** 3–4 小时；涉及 4 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- docs/error-recovery-log.md
- docs/eval-annotation-guide.md
- reports/evals/本轮报告（拟新增产物）
- reports/demo/本轮验收记录（拟新增产物）

**验收条件：**

- [ ] B01 两类错误和 B02 真人审批保留 trace、决定、工具成功结果与业务前后值；脚本化策略不得署为真人批准。
- [ ] 执行完整评测，记录所有失败/未执行项/重试及缺失计量；新版范围与原 35 条视图分开，原报告保留。
- [ ] 满足总体规划第 6 节建议门槛；安全不变量任一失败则阻止收口，模型退化如实归因并回到对应任务。

**验证：** 先 uv run pytest、uv run ruff check . 及前端 npm test/npm run build；再 uv run python -m evals --budget 0.1 --timeout 30。B01/B02 按冻结脚本在临时库运行；若 runner 尚不能承载真人流程，用独立验收记录，不虚构命令。

**交付物：** 完整报告、真实模型自愈观察与真人审批验收记录。

## T12：整理阶段交接与下一轮决策输入

- [ ] T12 完成并附验收证据。

**优先级/建议负责人：** P1 / 项目负责人/文档；实际负责人待分配。

**依赖：** T11、C4 证据齐全。

**范围与目的：** 把实际交付能力、运行边界和未完成项同步回项目入口，提供下一阶段框架/部署评估的依据。

**预计投入：** 2–3 小时；涉及 5 个文件或产物入口。超出约 5 个文件或单次专注开发时段时继续拆分子任务，保留本任务编号作为父项。

**可能涉及文件：**

- README.md
- docs/agent-project-plan.md
- docs/write-reliability.md
- docs/plans/恢复验收交接.md（拟新增）
- tasks/todo.md

**验收条件：**

- [ ] 按实际证据更新 M4 未勾选项，列出启动、故障演示、恢复、保留数据和版本不兼容处理方法。
- [ ] 验收矩阵每项链接有效证据；缺失项保持未完成，历史报告与原 ADR 不覆盖。
- [ ] 记录 LangGraph/PostgreSQL 后续评估问题与部署前认证/归属/审计门槛；继续声明本机单用户，不称生产就绪。

**验证：** 复核文档相对链接、任务状态与报告一致性；按交接文档从临时库独立复现一次完整批准恢复和拒绝零变化流程。

**交付物：** 可复核的阶段交接文档与后续决策清单。

### 检查点 C4：阶段完成

- [ ] T10–T12 与 R01–R10 全部验收；真实行为证据独立记录，无未经批准/重复写入/虚报成功。
- [ ] 项目负责人审阅证据后进入下一批次；问题回到对应任务，不能仅凭总通过率放行。
