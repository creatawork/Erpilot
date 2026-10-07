# AI 回答与过程展示：阶段二代码审查

日期：2026-10-07

审查对象：当前工作区未提交的两阶段实现，基于 `6a12414` 的改动及新增文件。

初审结论：需要修改。已确认 2 项 P1、4 项 P2；现有测试通过不能证明恢复与结果展示满足方案。
修复状态：2026-10-07，以下 6 项已修复并补充回归验证，详见文末修复记录。

初审没有修改产品代码。复现使用临时数据库、InMemorySaver、脚本化模型及 React 服务端渲染，不调用真实模型，不操作业务库。

## 1. [P1] 会话事件按运行内序号全局排序，导致多轮回答串轮

位置：[presentation.py:27](E:/workspace/erpilot/apps/api/src/erpilot_api/presentation.py:27)、[run_store.py:258](E:/workspace/erpilot/apps/api/src/erpilot_api/run_store.py:258)。

`next_presentation_seq(run_id)` 为每个运行分别从 1 计数；读取会话日志却仅使用 `ORDER BY seq`。投影再按 seq 排序，并把所有事件交给最近一个 user_message 的 current，未按 run_id 路由。

复现：同一 session 连续发送 question 1、question 2，模型分别返回 answer for question 1、answer for question 2。checkpoint messages 正确，但 presentation 的第一轮 blocks 为空，第二轮正文变成 `answer for question 1answer for question 2`，并采用某一运行的 done 数据。刷新后用户看到的回答归属错误。

建议：先按 run_id 分组，在运行内按 seq 回放；运行之间用稳定创建顺序排序。不要用随机 run_id 字典序或秒级时间戳作为唯一排序依据。增加至少两轮含工具调用且事件数不同的快照回归测试。

## 2. [P1] 恢复完成后仍使用失效展示投影，可能保留错误审批并锁住输入

位置：[session.ts:351](E:/workspace/erpilot/apps/web/src/session.ts:351)、[service.py:386](E:/workspace/erpilot/apps/api/src/erpilot_api/service.py:386)、[presentation.py:56](E:/workspace/erpilot/apps/api/src/erpilot_api/presentation.py:56)。

只要 presentation 非空，前端就完全优先使用，不用 checkpoint 的 messages、tool_results、pending_approvals 和 status 校正。两个已复现的触发路径：

1. 模型第一次返回 503，随后通过 stream_retry 恢复成功。失败路径的 fail_run 清空 active_run_id，恢复段 message=None，因此不创建 logger。快照 status=completed、messages 含 recovered answer，但 presentation 仍是最初的错误，没有恢复后的答案。
2. 在 ApprovalPending 处显式 aclose 服务流，GeneratorExit 被记录为 terminal error。恢复批准后，工具实际执行一次并完成；后续事件虽已落库，但投影因 terminal=True 全部忽略，仍保留 waiting_approval 和空 approvalResolved。前端 hydrate 后仍为 awaiting_approval，App 的 hasPending 因旧审批为 true，禁止发送新消息。

第二个复现验证的是服务流显式关闭路径，不等同于断言所有 HTTP 断连都产生 GeneratorExit；当前 HTTP CancelledError 已有不同处理。但真实模型失败恢复的第一条路径同样确认了展示与 checkpoint 不一致。

建议：给恢复段保留稳定运行关联；区分一次连接中断和整个运行终结；允许恢复段推进投影。快照生成或消费时必须以 checkpoint 校正实际完成结果与待审批列表，不能让展示投影决定审批是否仍有效。增加错误恢复后刷新、显式关闭审批流后恢复并刷新、投影落后 checkpoint 的测试。

## 3. [P2] 部分展示日志会覆盖完整历史，旧会话内容消失

位置：[session.ts:357](E:/workspace/erpilot/apps/web/src/session.ts:357)。

只要 presentation 包含一个轮次，就返回其中全部轮次，不合并 messages 中没有展示日志的历史。启用此功能前的旧消息没有日志；30 天清理后也可能只保留近期轮次。这时刷新只显示近期记录，旧消息虽然还在 messages 中却不可见。

复现：messages 含 old question/old answer 和 new question/new answer，presentation 只有 new 轮次；hydrateTurns 仅返回 new 轮次。

建议：以完整会话历史为基础，按稳定运行或消息关联增强已有轮次，而不是整体替换；没有投影的轮次走旧消息转换。覆盖旧会话新增消息、部分日志被清理、只有一轮投影缺失的测试。

## 4. [P2] 业务失败与未知结果仍被标记为成功

位置：[session.ts:184](E:/workspace/erpilot/apps/web/src/session.ts:184)、[presentation.py:88](E:/workspace/erpilot/apps/api/src/erpilot_api/presentation.py:88)。

实时 reducer 与持久化投影都仍使用 ok 判定 succeeded/failed，仅特殊处理审批拒绝，没有采用 display.outcome。MCP 业务错误可正常返回，ok=True 且 display.outcome=failed，此时工具摘要仍显示绿色勾和“已完成”。unknown 结果也会被当作成功。

复现：给 adjust_stock 的 tool_finished 传入 ok=True、业务 not_found 和 display.outcome=failed，实体 status 最终为 succeeded。

建议：统一结果分类契约，优先保留 denied，并使用经过验证的业务 outcome。无 display 时检查已知业务错误契约，无法确认则为 unknown。实时、投影和旧快照转换保持一致，避免前后端分别维护相互矛盾的规则。

## 5. [P2] 错误、未知和不受支持的卡片吞掉原始结果

位置：[BusinessResultCard.tsx:13](E:/workspace/erpilot/apps/web/src/components/BusinessResultCard.tsx:13)、[AssistantMessage.tsx:57](E:/workspace/erpilot/apps/web/src/components/AssistantMessage.tsx:57)。

isUsableDisplay 只检查 version=1，因此调用者不渲染原始结果；BusinessResultCard 却要求成功字段存在，否则返回 null。后端的 failed、denied、unknown 载荷通常缺少 quantity、quantity_after 或 order_id，因此结果区为空。未知 kind 也有同样问题。

React 服务端渲染复现：stock_adjustment 的 failed/unknown，以及 future_kind，均被 isUsableDisplay 接受，但最终 HTML 是空字符串。

建议：让可用性判断完整验证 kind/outcome/必需字段；错误与拒绝可单独渲染结果说明，不要求成功数据。任何无法展示的载荷必须回退原始 content。增加组件级失败、未知版本、未知 kind 和字段缺失测试，不能只测 reducer 保存了 display。

## 6. [P2] 工具参数为合法 JSON null 时，消息渲染抛错

位置：[session.ts:292](E:/workspace/erpilot/apps/web/src/session.ts:292)。

toolSummary 仅捕获 JSON.parse 异常，随后直接访问 args.sku。JSON 字符串 `null` 解析成功，但会触发 TypeError。graph prepare 在校验前已发出 ToolCallStarted，因此错误模型调用仍会进入 ToolCard；后端本来能够返回参数校验错误，前端却先发生渲染异常。

复现：toolSummary({...entity, arguments: 'null'}) 抛出 `Cannot read properties of null (reading 'sku')`。

建议：解析后检查非空对象并排除数组，再读取字段；无效参数显示空摘要与原始详情。补充 null、数组、标量和非法 JSON 的输入边界测试。

## 验证记录与覆盖边界

- `apps/web` 中运行 `npm test`：32 项通过。
- `apps/web` 中运行 `npm run build`：TypeScript 检查及 Vite 构建通过。
- 运行 `.venv-langgraph/Scripts/python.exe -m pytest apps/api/tests packages/agent_core/tests -q`：退出码 0，输出中有 3 项跳过。
- 临时 Python 脚本通过真实 ChatService/RunStore/图运行时复现多轮串轮、模型错误恢复失效投影、显式关闭审批流后的冻结投影；确认工具仅执行一次。
- 临时 Node 脚本执行实际 reducer/hydrator，并使用 React 服务端渲染复现空白卡片、错误成功状态、部分历史丢失及 null 参数异常。

本次没有运行真实浏览器或供应商模型，因而不对端点实际思考增量、浏览器首包延迟、用户滚动和移动端视觉验收作结论。已有相关截图不替代上述运行时验证。

优先修复跨轮事件归属和恢复投影校正，再修复结果分类、卡片回退与输入边界；把这些复现场景加入回归后再验收阶段二。

## 修复记录（2026-10-07）

1. 持久化日志按插入顺序读取，按 run_id 分组、运行内 seq 排序回放，避免运行局部序号相同导致串轮。
2. 错误恢复重新关联原运行，新增 resume 边界重新打开投影；取消连接及 GeneratorExit 不记为运行终结。前端用 checkpoint 的完成结果、状态和待审批列表校正投影，清除失效审批。
3. 完整 messages 历史作为恢复基础；用 user_index 关联新日志，旧日志按问题及轮次顺序匹配。部分日志缺失不隐藏旧历史，日志落后时保留已提交工具块。
4. 实时、日志投影和旧快照均识别业务 error、审批拒绝及版本化 outcome；已知业务工具缺少可确认成功的结果字段时显示 unknown。checkpoint 工具历史保留 display，日志清理后仍可恢复业务结果状态。
5. 卡片入口校验 kind、outcome、字段类型和有效数值。成功契约完整才显示业务卡片；失败、拒绝、未知以及不兼容载荷回退原始 content，保留详细结果。
6. 参数解析后检查非空对象、排除数组；null、标量、数组及非法 JSON 显示空摘要，不抛出渲染异常。

回归验证：

- 前端 `npm test`：43 项通过，包含实际 React 组件渲染的原始结果回退测试。
- 前端 `npm run build`：TypeScript 与 Vite 构建通过。
- 项目全量 `python -m pytest`：249 项通过、3 项跳过、35 项 eval 按项目配置排除。
- 本次修改涉及的 Python 源码与测试通过 Ruff 检查。
- 真实 Edge 浏览器加载生产构建，使用合成会话快照验证旧历史保留、失效审批清除、失败原始结果可见、null 参数不崩溃、输入框恢复可用；无 pageerror。截图：`C:/Users/V/AppData/Local/Temp/erpilot-repair-browser.png`。

边界：浏览器验证使用模拟 API 数据；真实供应商推理增量与被跳过的 PostgreSQL 集成用例未在本次验证。

## 本地体验问题跟进（2026-10-07）

用户体验测试发现思考不可见、库存前后不一致。核对运行日志后确认本次启动的旧 `.env` 缺少工具来源、写入及思考配置，导致 API 使用只读 demo 工具（`check_stock` 固定返回 18），思考增量被配置过滤。两次所谓出库都没有 `adjust_stock` 调用，不能把模型预测的 8 件当作操作结果。

原库存 9 件是 11:02 的已完成写操作；另一会话在 14:27 真实入库 9 件，工具返回库存 18。真实 ERP 的随后只读查询也确认数量 18。历史回答不会被改写，出库是否生效以审批后的写工具结果为准。

本地 `.env` 已恢复 MCP、审批写入、思考及 checkpoint 配置；运行中思考默认展开，新增实际数据源／写入能力提示和新会话入口，接收 start 时立即更新回答状态。完成摘要改为“回复完成”，避免将模型收口误读为业务操作成功。提示词要求写请求调用写工具、未执行时不得报告操作后库存或猜测变动原因。

真实 Edge 浏览器经当前前后端调用实际模型验证：发送后立即显示状态，2 秒时思考增量已展开，最终仅调用查询库存工具并返回真实库存 18，未执行写操作、无页面异常。截图位于 `C:/Users/V/AppData/Local/Temp/erpilot-live-reasoning.png` 与 `erpilot-live-final.png`。新增组件与运行能力回归测试，前端 44 项通过及构建通过。

全量回归首次暴露原有 API 单测受本地 `.env` 模式影响，已改为显式注入模拟工具／空工具，不让模拟 API 测试连接真实 ERP。最终全量 Python 回归 250 项通过、3 项跳过、35 项 eval 按配置排除；涉及 Python 文件通过 Ruff 检查。
