# 当前阶段：功能深化 B5 · 政策与订单事实联合判断（工程验收完成，真实模型待测）

2026-10-12：用户授权当前会话实现 B5。政策元数据、有效检索、只读联合判断、展示卡和恢复链路已实现；Python 636 项、前端 77 项、浏览器 12 项及构建通过，见 [B5 工程验收](../reports/acceptance/2026-10-12-policy-business-decision.md)。独立审阅正在收口；真实模型与 M06 计费 / 对照支线未运行。工作台整体改版仍有既有未提交文件，远端 CI 未验证。

- [x] 核对当前工作台协议 / 组件测试、生产构建及浏览器回归；保留首轮浏览器缺失与指定本机 Chromium 后通过的记录。
- [x] CI 增加匹配版本 Chromium 安装、浏览器测试及失败证据上传入口。
- [ ] 验证远端 CI，并完成当前工作台改版的整体审阅与独立提交。
- [x] 用户确认按 B5 书面规格继续制定实施计划。
- [x] 形成独立计划与下列小任务；不将规划完成标为功能完成。
- [x] 用户授权按 B5 主线计划在当前会话逐项实施；M06 支线独立待办。
- [ ] 补 B4 真实模型多轮指代与澄清评测；确定性与模型结果分开统计。
- [ ] 补当前端点 DeepSeek 美元计量与预算契约，再执行 GLM / DeepSeek 对照。

## B5 工程主线

范围、接口、失败测试与验证命令见[B5 实施计划](../docs/superpowers/plans/2026-10-12-policy-business-decision.md)。工程任务已验证；真实模型评测单列待办。

- [x] B5-01：政策版本、生效区间与正文哈希元数据；无时区 / 哈希错误拒绝，LF 与 CRLF 一致。验证 `uv run pytest packages/rag/tests`。
- [x] B5-02：有效版本与规则小节检索；多小节不误判冲突，过期片段与重叠版本不误放行。验证 `uv run pytest packages/rag/tests`。
- [x] B5-03：逐片段核对后刷新索引元数据，不重复嵌入；变化正文拒绝复用。验证新 index_refresh 测试和实际索引副本。
- [x] C1：证据基础测试、Ruff 和差异检查通过；旧索引不能作为有效引用。
- [x] B5-04：search_policy 返回版本和有效时间，检索阶段异常结构化返回。验证 MCP 政策测试。
- [x] B5-05：联合判断纯函数直接复用 CANCELLABLE_STATUSES，六状态与证据缺口准确。验证判断与原 operations 回归。
- [x] B5-06：assess_order_cancellation 在所有工具面注册，无 session_id、风险审批或 ERP 副作用。验证真实 MCP、全表快照及工具卡同步。
- [x] C2：只读业务闭环通过，原取消审批、幂等及写风险门槛保持。
- [x] B5-07：严格校验的版本化 display，非法字段回退详情，agent_core 不导入 ERP / RAG。验证 display / presentation 回归。
- [x] B5-08：只读判断卡与可展开引用；无取消按钮、危险链接或伪造金额。验证 `npm test`、`npm run build`。
- [x] B5-09：API / CLI 指引、澄清、旧提示恢复与明确取消后的原审批链路。验证 API / CLI 脚本客户端与 operations 回归。
- [x] B5-10：浏览器与工程验收，P01–P14 有证据映射；全仓检查通过并明确模型评测状态。
- [x] C3：确定性工程门槛全部满足，真实模型未运行时仍保留未完成项。

## M06 计费与真实模型对照支线

详见[支线计划](../docs/superpowers/plans/2026-10-12-model-comparison.md)。B4 真实多轮指代仍是独立待办，不用 B5 单轮试跑替代。

- [ ] M06-01：按当前端点以 Decimal 保存原币种价目；USD 不冒充 CNY，缓存计量未知时保守估算。验证 billing / prices 单测。
- [ ] M06-02：每次模型请求前的预算检查、usage 缺失熔断与 CaseResult.billing 增量。验证 billing / runner HTTP mock。
- [ ] M06-03：CLI、基线与报告明确预算币种；保留旧 cost 的人民币契约。验证 baseline / report 回归。
- [ ] C-M1：价目、币种、预算、失败计量与重试策略离线通过，普通 pytest 不调用真实模型。
- [ ] M06-04：7 项 B5 模型用例与结构化证据 / 全表状态检查，单列 b5-1 检查，不修改旧 scorer。
- [ ] M06-05：strict 对照校验源码、案例、scorer、seed、重试及政策指纹；不同币种不混算。
- [ ] M06-06：GLM / DeepSeek 各留 B5 试跑与现有完整 baseline 报告，失败如实保留；完整基线未完成时不勾选 M04。

## 已完成阶段：B4 · 多轮草稿任务与历史会话

2026-10-11：B1–B4 确定性业务链路已完成；B4 新增持久草稿任务查询、按版本和行号修改数量、历史会话分页与安全恢复，详见 [验收报告](../reports/acceptance/2026-10-11-task-history.md)（513 passed / 21 skipped / 47 deselected；前端 73 测试与构建通过）。真实模型指代准确率尚未评测。继续按 [业务里程碑与后续路径](../docs/business-roadmap.md) 推进 B5 政策与业务事实联合判断，再进入补货建议；暂不考虑部署。

## 上一阶段：批次 2 · RAG 政策问答（批次 1 待 key 收尾）

更新日期：2026-10-09。目标：在现有 loop 上接入**只读检索**的政策问答（价格/折扣/退换货），回答基于检索片段并给出来源，语料外问题如实拒答。优先 JD 硬指标（多模型 + RAG），深度优先，不设固定发布日期。

- 下一阶段总体规划：[plan-next-phase.md](plan-next-phase.md)（含批次 3–5：评测叙事、部署 demo、文章）。
- 批次 3（C3）已于 2026-10-09 完成验收：趋势图与机器可读汇总已重建；五类注入面均映射到真实对照 case；v2.6 离线复判保留原始 v2.5 运行报告。详见[验收复判](../reports/security/20261009-prompt-injection-v26-rescore.md)。趋势数据当前为 GLM 单模型样本；跨模型对照仍属批次 1 待办。
- 上一轮「基线验收与失败归因」存档：[2026-10-08 基线存档](todo-2026-10-08-baseline-archive.md)。
- 历史恢复协议规划：[plan.md](plan.md)；长期路线：[启动计划](../docs/agent-project-plan.md)。

当前执行边界：本机、可信单用户；保持 `agent_core` 零业务依赖；RAG 以只读检索工具接入，不新增业务写能力。

## 批次 2 待办（C2：检索接入 loop；grounded 回答 / 语料外拒答 / 引用来源）

- [x] R01 / P0：新建业务无关检索引擎包 `packages/rag`——`chunk`（标题分块）+ `embed`（`Embedder` 协议 + 余弦）+ `index`（`PolicyIndex` 余弦 top-k + 阈值拒答 + JSON 持久化）。14 条离线单测（fake embedder，不烧 token）。
- [x] R02 / P0：政策语料 3 份（价格/报价、订单/取消、退换货）固定入库 `packages/rag/.../policies/*.md`，折扣档位/状态机与 `erp_store` 口径一致；`corpus.load_corpus` 加载分块；真实嵌入器 `OpenAIEmbedder`（默认智谱 embedding-3，`EMBED_*` 可覆盖）+ 构建入口 `python -m rag build`。10 条离线单测（含语料规则校验、嵌入器 mock）。
- [x] R03 / P0：`search_policy` 只读 MCP 工具接入 `mcp_erp`（返回片段 + source/title + score；空命中返回拒答提示；索引未built 返回 `policy_unavailable`）；`policy_resolver` 可注入贯穿 `create_server`/`bridge`；系统提示词补「先检索政策、依据片段作答注明来源、无命中如实拒答不臆造」。5 条测试（server 级 + 桥接端到端，fake embedder）。只读工具 16→17、写面 20→21，工具卡同步。
- [x] R04 / P0：评测新增 RAG 子集——命中 grounded 回答、语料外拒答、引用正确来源、注入片段不改写意图；单独统计。**依赖 R05 阈值校准**（否则可能因阈值而非模型失败）。真实完整验收 5/5 通过，见 `reports/rag/20261009-150829-full-acceptance.md`。
- [x] R05 / P1：`python -m rag build` 构建真实索引（消耗嵌入 token），用真实嵌入校准 `POLICY_SCORE_THRESHOLD`（现为 0.66）正/负例；存储/嵌入选型取舍写入报告或 README；留一次真实检索链路证据。校准 12 条（6 正、6 负），真实链路证据见 `reports/rag/20261009-150829-full-acceptance.md`。

说明：R01–R05 已完成。2026-10-09 已配置百炼 DashScope `text-embedding-v4`，构建 18 个政策片段索引；为适配模型每批最多 10 条的限制，`OpenAIEmbedder` 已加分批提交，RAG 离线测试 25 条通过，真实校准及 RAG 完整链路验收 5/5 通过。详见[真实验收记录](../reports/rag/20261009-real-acceptance.md)。`search_policy` 在索引未构建时对模型返回 `policy_unavailable`（优雅降级），不影响现有链路启动。

## 批次 1 剩余（当前 key 支持 DeepSeek；真实运行消耗 token）

- [ ] M04 / P0：优先使用当前中转端点与 key，对照 GLM-5.3 Flash / `deepseek-v4.1-flash`；Qwen 可用配置另行核验。使用相同基线并保留独立报告，`python -m evals.compare` 产出对照表；不能拼接不同 scorer 或用例集。
- [ ] M05 / P1：对比结论写入报告 + README 多模型对照入口；标注端点差异为真实观察。
- 遗留：当前端点 DeepSeek 价目已由用户确认（USD / 百万 token：输入 0.045、输出 0.18、缓存读取 0.024、缓存写入 0）；现有 `prices.py` 和预算为人民币，M04 前须补齐币种 / 换算契约，不能把美元直接写为人民币。Qwen 价目仍待核验。

已交付（批次 1）：M02 provider profile 层（zhipu/deepseek/qwen，默认兼容，7 单测）；M03 runner 已原生记录模型维度 + `evals/compare.py` 对照聚合器（7 单测）。

## 验证入口

- `uv run pytest`、`uv run ruff check .`
- 切供应商：`.env` 设 `LLM_PROVIDER=deepseek`（填 `DEEPSEEK_API_KEY`）或 `=qwen`（填 `DASHSCOPE_API_KEY`）。
- 跨模型对照：`uv run python -m evals.compare`。

判定规则：报告产出与问题归因可以完成；运行门槛有失败时如实标未通过。RAG 语料外臆造答案、跨模型拼成虚构统一成功率均零容忍。
