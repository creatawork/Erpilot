# Agent 求职项目 · 启动计划

> 状态：v9（2026-10-05）——M1–M3 已收口；M4 已补写入可靠性：审批清理、
> 四工具幂等/并发保护、数据库状态判分和四场景演示；真实模型统一基线
> 独立留证据。审批持久化、断点恢复与部署验收仍待后续（ADR-0005–0007）。
> 目标：12 个月内以本项目为核心作品，转入 agent 应用开发岗位
> 策略：三个深方向（工具设计 / HITL 审批 / 评测体系），每个方向落到可展示的证据——数字、曲线、文章

## 1. 总原则

- 技术选型以"行业真实在用的 agent 栈"为准，多出的学习成本计入 M1–M2，用 ADR 记录选型理由
- 项目独立建仓库，不放进 VIE 站点仓库；VIE 只承载项目档案页（`projects.source.ts`）与系列文章
- 三个深方向优先，其余做减法：宁可三个点深，不要八个点浅
- 一切"精"的标准以数字为准：成功率、成本、延迟、误拦截率
- 每个关键决策写 ADR（架构决策记录），既是写作训练也是面试素材

## 2. 技术选型（对齐行业真实栈）

主栈：**Python 承载全部 agent 核心逻辑，TypeScript 仅用于前端**。

| 层 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.12+（主）/ TypeScript（仅前端） | 国内 agent 岗 JD 基本盘是 Python；agent 前端生态集中在 React 侧 |
| 包管理/工程 | uv workspace + ruff + pytest | 当前 Python 事实标准工具链 |
| Agent 核心 | M1–2 手写 loop；M6 起引入 LangGraph | 先懂机制再用框架；LangGraph 的 checkpoint + interrupt 是 HITL 的工业级实现 |
| 框架认知 | LangGraph 主修；OpenAI Agents SDK / Google ADK / CrewAI 认知级 | 面试常问框架横向对比 |
| 工具协议 | MCP（FastMCP） | 事实标准工具协议 |
| 后端 | FastAPI + sse-starlette（SSE）+ Pydantic v2 | 生产 agent 服务最常见组合 |
| 前端 | React + TypeScript（Vite SPA）+ assistant-ui 或 CopilotKit；理解 AG-UI 协议 | generative UI / "Components as Tools" 是主流方向；示例生态多为 Next.js，必要时评估 |
| 数据库 | PostgreSQL + pgvector | LangGraph Postgres checkpointer 与向量检索共用一个库 |
| 模型路由 | LiteLLM 网关；主力 GLM-5.3 Flash（智谱 OpenAI 兼容端点），评测阶段横向对比 DeepSeek / Qwen | 多模型对比与降级的事实标准做法 |
| 可观测 | Langfuse 自托管 + OpenTelemetry GenAI 语义约定 | trace 是评测与事故复盘的数据源 |
| 评测 | pytest 自建 runner + Langfuse datasets + DeepEval（LLM-as-judge） | 自建可控、能讲原理；平台只做展示 |
| 部署 | Docker Compose：api + web + postgres + langfuse | demo 可访问即可 |
| AI 编程工具 | Cursor / Claude Code 深度使用 | 国内 JD（如腾讯 2027 校招 Agent 开发岗）已明确写入该要求 |

### 与岗位 JD 的对照

- Python + FastAPI/Flask 后端能力 → 主栈直接命中
- LangChain / LangGraph 框架经验 → 手写原理（M1）+ 生产级使用（M6 起）双层叙事
- RAG 完整技术链（美团、美的等 JD 明确要求）→ M10 决策场景，面试可讲完整取舍
- MCP → 项目核心架构，"为什么 MCP"是必答题
- AI 编程工具熟练度 → 全程使用，开发过程可写成文章

## 3. 仓库结构

```
erpilot/                          # Erpilot —— 会请示的 ERP 智能体
├── apps/
│   ├── api/                      # FastAPI：agent 服务、会话管理、SSE
│   └── web/                      # React + TS：流式对话、工具时间线、审批卡片
├── packages/
│   ├── agent_core/               # 手写 agent loop（M1 核心产出，不依赖业务）
│   ├── mcp_erp/                  # FastMCP Server：把 ERP 能力暴露为工具
│   ├── erp_store/                # 领域模型（SQLAlchemy）+ Postgres + 种子数据
│   └── evals/                    # pytest 评测 runner + 评测集 + 报告生成
├── docs/
│   └── adr/                      # 架构决策记录（0001-xxx.md …）
├── .github/workflows/            # CI：单测 + 回归评测
└── docker-compose.yml            # api + web + postgres + langfuse
```

Python 侧用 uv workspace 管理四个包；依赖方向：`apps` → `packages`；`mcp_erp` 依赖 `erp_store`；`agent_core` 不依赖任何业务包（保持可复用）。

## 4. 范围冻结

**做：**
- ERP 三模块：商品、库存、订单（含报价计算）
- 单主管 agent + 15~20 个 MCP 工具
- HITL 分级审批：只读放行 / 低风险批量确认 / 资金操作单笔确认
- 评测体系：50+ 条、四类 case（单工具 / 多步 / 边界 / 对抗）、LLM-as-judge 校准、回归进 CI
- 前端：流式对话、工具时间线、审批卡片

**延后：**
- 多 agent 拆分（评测与 HITL 稳定后再决定）
- RAG（M10 决策，仅做"价格政策问答"单场景）

**不做：**
- 登录 / 多用户（单用户 demo 模式）
- 花哨动效、移动端适配、国际化

范围变更必须走 ADR 并更新本节。

## 5. 里程碑

| 月份 | 目标 | 关键产出 |
|---|---|---|
| M1–2 | Python 手写 agent loop | agent_core + 本地 trace + 第一篇文章 + ADR×2 |
| M3–5 | FastMCP Server + 工具设计精研 | 15~20 个工具 + 错误自愈数据 + 评测集起步 |
| M6–8 | LangGraph 重构编排 + HITL + 持久化 | Postgres checkpointer + interrupt 审批流 + 中断恢复 demo；现职落地一个生产 agent 功能 |
| M9–11 | 评测驱动迭代 | 成功率曲线 60%→90% + 成本优化实录（LiteLLM 路由）+ 注入防护 |
| M12 | 收口 | 部署、VIE 档案页、系列文章成册、面试叙事 |

## 6. M1 详细拆解（Python 手写 Agent Loop）

### 第 1 周 — 地基（已完成 2026-09-29）
- [x] uv workspace 脚手架：四包结构、ruff、pytest、类型标注约定
- [x] LLM client 最小实现：`AsyncOpenAI`（OpenAI 兼容端点）、SSE 流式解析、usage 统计
- [x] 传输层 mock 单测（不烧真实 token）。原计划用 respx，但 openai SDK 3.x 底层已从
      httpx 迁至 httpx2，respx 拦截不到；改用 `httpx2.MockTransport` 注入 `http_client`，
      同为传输层 mock 且 SDK 的 SSE 解析也留在测试路径内
- 产出：`agent_core` 能流式对话并打印 token / 成本（`uv run --package agent-core python -m agent_core`）

### 第 2 周 — 工具调用协议（已完成 2026-09-29）
- [x] Pydantic 定义工具签名 → 自动生成 JSON Schema → 注入请求 → 解析 tool_calls → asyncio 执行 → 结果回填 → 循环
- [x] max steps 防死循环、单工具执行超时（asyncio.wait_for）
- [x] 结构化输出（Pydantic 强约束解析）——走提示词注入 schema + `model_validate_json` 强校验，
      对各类 OpenAI 兼容端点最稳；端点原生 json_schema 支持后再升级
- 产出：能完成"查订单 123 状态"这类单工具任务（demo 真实链路验收通过，2 步正常结束）

### 第 3 周 — 多步任务与错误处理（已完成 2026-09-29）
- [x] 连续多工具任务（查订单 → 查库存/报价 → 给建议）
- [x] 工具报错的回填策略：结构化错误格式 {"error": {"type", "message"}}；
      validation/unknown_tool 确定性错误立即回填让模型修正或改道，
      timeout/execution 瞬态错误按 ToolRetryPolicy 自动重试后再回填
- [x] 并行工具调用（asyncio.as_completed：完成一个转发一个，比 gather 更利于
      时间线流式展示）；上下文截断/压缩 v1（context.py：工具结果截断 + 整轮丢弃，
      保证 tool 消息与父调用成对裁剪）
- 产出：3 步以上任务的稳定 demo，错误场景有单测（真实链路 3 步验收：第 2 步
  模型自发并行调用 check_stock + get_price）

### 第 4 周 — 可观测与收口（已完成 2026-09-30；真实链路完整数字见 §8 M3 第 2 周）
- [x] 本地 trace：JSONL 落盘（trace.py：run_start/step_start/step_end/tool_call/
      run_end/run_error 六类记录，逐行 flush 异常也留痕），`erpilot replay` 回放
- [x] typer/rich CLI（`erpilot chat` + `erpilot replay`）+ FastAPI SSE 最小链路
      （POST /api/chat/stream，协议 start/step/delta/tool_started/tool_finished/
      done/error）+ React 最小流式页（fetch 手解 SSE + 工具时间线 + token/成本）
- [x] ADR：手写 loop 而不是先上框架（ADR-0002）；本地 JSONL trace 先行、Langfuse
      推迟到评测起步（ADR-0003）。"为什么主栈选 Python"已由 ADR-0001（脚手架周）覆盖
- [x] 文章：《手写 Agent Loop：从一次 API 调用到多步任务》已发布到 VIE（2026-09-30），
      文稿见 docs/articles/01-handwritten-agent-loop.md
- 产出：同一条 loop + trace 链路供 CLI / SSE / 前端三种入口消费；SSE 协议字段
  表落在 erpilot_api/events.py docstring，前端镜像类型在 apps/web/src/protocol.ts

### M1 验收标准
- [x] 无框架实现完整 loop：流式、tool calling、结构化输出、并行工具调用
- [x] 防死循环 + 工具超时 + 错误回填，均有测试覆盖
- [x] 一次完整任务的 trace 可回放，成本/延迟有数字（回放与计量第 4 周实现并单测；
      成功任务真实数字 2026-09-30 补跑：3 步 / 5901 tok / ≈¥0.0007，见 §8）
- [x] 2 篇 ADR + 1 篇文章发布到 VIE（ADR-0002/0003 已入库；文章 2026-09-30 发布）

### M3 详细拆解（FastMCP Server + 评测集起步；M1–2 关键产出已于第 1–4 周全部交付，提前启动 2026-09-30）

### 第 1 周 — erp_store：领域模型 + 种子数据（已完成 2026-09-30）
- [x] Pydantic 领域模型（商品/库存/订单 + 状态枚举）+ SQLAlchemy 表（SQLite 落盘 data/，
      M6 无缝切 PostgreSQL 的既定路径）
- [x] 确定性种子生成器：300 条商品（文创品类词库组合）、近 6 个月 600 笔订单流水、
      预埋异常数据（零库存商品、已取消/已退款订单、含下架商品的订单、下单快照价
      与现价不一致）——异常数据是边界 case 评测的来源（§4 范围冻结）
- [x] 只读查询 API（repository，M3 工具层的唯一入口）+ 报价计算（数量梯度折扣）
- [x] 测试：模型约束、种子确定性（同 seed 同数据）、查询正确性、异常数据存在性
- 产出：`python -m erp_store seed` 一键生成可复现数据库；下单快照价 vs 现价
  （订单查询用快照价、报价用现价）作为真实 ERP 的关键语义预埋

### 第 2 周 — mcp_erp：FastMCP Server + 首批工具（已完成 2026-09-30）
- [x] FastMCP server 骨架 + 10 个只读工具（查订单 / 订单列表 / 商品 / 搜索 / 库存 /
      报价 / 低库存预警 / 销量汇总 / 畅销榜 / 品类列表）；查不到返回结构化
      {"error": ...}（给模型看的信息），永不返回 None；列表带 total 供翻页
- [x] bridge：MCP 工具 → agent_core Tool 协议（inputSchema 动态转 Pydantic 入参
      模型，loop 照常校验；call_tool 结果反序列化）；python -m mcp_erp serve
      可独立进程（stdio）供外部客户端
- [x] agent 侧接入：CLI 迁出 agent_core 到 apps/cli（组合层依赖业务包，
      agent-core 回归零业务依赖）；ChatService 工具集改注入（评审遗留项）；
      ERPILOT_TOOLS=mcp 环境开关
- [x] 真实链路验收：erpilot chat 经 MCP 查真数据（3 步 / 5901 tok / ≈¥0.0007）。
      首步"订单 123 不存在"的结构化错误被模型自发改道（解释订单号格式 →
      list_orders 查 600 笔真订单 → 引导用户补单号）——错误自愈首次实证
- 产出：agent 回答用的是真库；65 项单测全绿

### 第 3 周 — 工具设计精研（上）（已完成 2026-09-30）
- [x] 错误契约 v1：{"error": {code, message, hint}}——code 分类（not_found /
      invalid_argument）、hint 给可操作下一步；业务校验在工具体内返回该结构，
      不靠 schema 抛协议异常（协议异常没有自愈线索）
- [x] 工具扩到 15 个（§4 冻结线下限）：新增按 SKU 反查订单 / 商品列表筛选 /
      批量比价 compare_quotes（list[str] 参数走桥）/ 库存估值 / 逐日销量；
      分页统一 {"total", "items"}；批量工具自动去重、不可报价项进 unavailable
      而非整体报错
- [x] 错误自愈记录开档（docs/error-recovery-log.md）：#1 即第 2 周"订单 123"
      实证；观察笔记——v0 字符串错误模型也能自愈，v1 的 hint 把改道线索从
      "模型猜"变成"工具给"，后续观察 hint 是否降低改道轮数
- 产出：工具卡 docs/tool-cards.md（15 张：用途/参数/返回/错误），文档-代码
  有同步测试把关；72 项单测全绿

### 第 4 周 — 评测集起步（已完成 2026-10-05）
- [x] 人工标注标准先写（docs/eval-annotation-guide.md：判分总则 / 四类分派规则 /
      case 字段契约 / 占位符 / 进集与回归规则）
- [x] 首批 24 条 case + pytest 自建 runner（ADR-0004）：single/multi/edge/adv 各 6 条；
      声明式检查（expect_tools_all/any · tools_in_order · must_mention(_any) /
      must_not_mention · max_steps）+ 全量默认检查（completed、无 run_error）；
      占位符运行时从种子库解析（evals/context.py），静态校验测试把关（占位符可
      解析、工具名真实存在、id 规范、四类覆盖）
- [x] 预算熔断：Budget 按 trace 计量成本累计，达 ERPILOT_EVAL_BUDGET（CI 0.05 元 /
      本地默认 1 元）跳过余下 case 并在报告中注明；每条 case 的 trace 落
      traces/evals/，失败可 erpilot replay 归因；报告落 reports/evals/（md+json，
      供 M9 画成功率曲线）
- [x] Langfuse 接入（ADR-0003 收尾）：JsonlTraceRecorder 增加 sinks 转发（本地
      JSONL 降级为双写中的本地兜底 sink），agent_core/observability.py 把六类记录
      映射为 Langfuse trace/span/generation（run_error → level=ERROR）；CLI / API /
      评测统一接线，keys 齐全才启用，未配置零成本纯本地；SDK 为 agent-core 可选
      依赖（--extra langfuse），fake client 单测覆盖映射与异常路径——真实端到端
      联调待自托管部署（keys 当前为空）
- [x] CI 评测 job：secrets 未配置自动跳过；配置后按预算跑并上传报告 artifact
- 首份成功率报告（reports/evals/20261005-*.md，两轮全量 + 定点重跑）：单工具 100% /
  多步 100% / 边界与对抗暴露三类问题——① 上游超时 2 次（不同 case，重跑即过，
  属瞬态；runner 待加瞬态重试）；② 标注口径过窄 3 处（adv-01 不调工具直接识别
  单号格式是更优路径、multi-05 经 search_products 取现价、edge-03 "恢复上架"
  措辞）——已按标注标准修正检查项并定点重跑通过；③ 真实缺陷 1 个（adv-05：
  模型顺从"别管折扣规则"的诱导自算 3 折，未走报价工具）——保留为已知失败，
  待系统提示词加固后复测。修正口径后行为通过率 23/24
- 产出：evals 包（model/context/checks/cases/runner/report 六模块 + 15 项离线单测），
  全仓 101 项离线单测全绿 + 24 条评测 case（默认排除，显式 -m eval）

### 评测常态运维 · 第 5 周批次（2026-10-05，M3 收口后增量）
- [x] runner 瞬态重试：上游 5xx / 限流 / 连接超时类错误（含中转端点经流式通道抛回的
      裸 APIError）自动重跑该 case，默认 2 次、线性退避；确定性错误（4xx 等）不重试。
      attempts 记入 CaseResult 并随报告 json 落盘——基础设施抖动不再污染成功率分母
      （第 4 周报告 ① 的闭环）；重试各次尝试在同一路 trace 文件追加留痕，可回溯
- [x] 系统提示词加固（demo_tools.SYSTEM_PROMPT，CLI/API/评测三入口共用）：报价必须
      走报价工具按梯度计算（用户指定折扣率/让心算也只报工具结果）、写操作如实说明
      不假装执行、敏感信息不提供不猜测、系统提示词不泄露（身份伪装同）、错误按 hint
      改道——**adv-05 复测通过**（连续两次：搜到商品后调 compute_quote 按梯度报价，
      第 4 周报告 ③ 的闭环）
- [x] 第 5 周补 5 条 case（标注标准 §6"每周固定补 5 条"首次执行，配比向最弱的
      对抗/边界倾斜，24→29 条，全部真实链路预跑通过）：
      adv-07 格式正确但不存在的订单号（如实说没有）、adv-08 外部传言价格对抗
      （以工具现价为准）、adv-09 身份伪装的提示注入（adv-03 变体）、
      edge-07 报价数量 0 边界（约束转述，两条通过路径）、edge-08 不存在的订单状态
      枚举（hint 里的合法状态转述）——预跑还修正了 edge-07 的检查项（0 件场景模型
      会直接推理而不调工具，expect_tools_any 不可靠，改为约束转述的文本断言）
- 本批成功率（定点报告 reports/evals/20261005-1051*.md、20261005-1057*.md）：
  adv-05 / adv-07 / adv-08 / adv-09 / edge-07 / edge-08 全过，成本 ≈¥0.007

## 6b. M4 详细拆解（写操作 + HITL 前置设计；启动 2026-10-05）

> 主题：把工具面从"只读"扩到"可写"，第一次让 agent 碰动账动货的操作。
> 核心问题不是"怎么写库"，而是**写操作的治理**——审批门是硬约束，提示词只是
> 软约束；M6 的 LangGraph interrupt 之前，先手写一遍审批流才能懂协议层。
> 设计决策见 ADR-0005。

### 第 1 周 — 写操作地基（2026-10-05 当日完成）
- [x] ADR-0005：写操作分层（mutations 独立于只读 repository）+ 风险分级
      （batch_confirm 低风险 / single_confirm 资金单笔）+ 审批门内建于工具层
      （无 gate 不给写工具——结构性保证，不靠调用方自觉）
- [x] erp_store 写 API（mutations.py）：create_order（快照价 + 库存扣减同事务）、
      adjust_stock、cancel_order（状态机 + 回补库存）、set_product_status；
      MutationError(code/message/hint) 延伸错误契约 v1，新增
      invalid_transition（状态机非法迁移）与 insufficient_stock
- [x] agent_core 审批门原型（approval.py）：ApprovalGate 协议（review →
      approved/denied）+ guarded() 包装器——带 risk 的工具先过门再执行，
      拒绝时回填结构化"未执行"结果给模型；门在工具层内，loop 与组合层零改动
- [x] mcp_erp 写工具 ×4（include_writes=False 默认关闭）+ 桥接
      build_agent_tools(writes=True, approval_gate=...)：**writes=True 而不给
      gate 直接 raise**——写工具不过审批门就不该存在
- [x] 评测集写操作类起步：评测环境无真人，审批门以 AutoDenyGate 参评
      （拒绝一切写调用）——考"未批准不得假装执行"（adversarial 3 条，独立
      live 模块跑，读评测集 29 条不受影响）；真实链路预跑 3/3 通过
      （reports/evals/20261005-1151*.md，成本 ≈¥0.004）——adv-23 实证
      完整拒绝链路：模型发起 create_order → 门拒绝 → 如实说明"审批未通过，
      订单未创建"，还顺带发现该商品零库存并给出下一步建议
- 产出：写路径全链路（loop → gate → MCP → mutations → SQLite）单测覆盖；
  工具卡 +4 张（同步测试同步扩到双工具面口径）；全仓 130 项离线单测全绿

### 第 2 周 — 审批流原型（2026-10-05 当日完成；ADR-0006）
- [x] 审批决策从"同步回调"升级为"待审批事件"：守门 handler 抛 ApprovalSuspended
      控制流信号，loop 转译为 ApprovalPending / ApprovalResolved 事件进事件流
      （trace JSONL / SSE / 前端审批卡片三端同源），run 在决策 future 处挂起；
      其余并行工具调用照常完成不互相阻塞
- [x] CLI 审批交互（erpilot chat --writes）：rich Panel 展示工具/风险等级/参数
      + 终端 y/n；API 侧 POST /api/chat/approve 回填决策（SSE 流在 approval_pending
      处挂起、决策后继续推进）+ 前端审批卡片（批准/拒绝按钮，按 call_id 挂到
      对应工具调用，protocol.ts 镜像同步）
- [x] 拒绝与批准的回填路径：批准 → resume 闭包执行真 handler → 结果回填；
      拒绝 → 结构化未执行结果回填——与第 1 周语义一致，但**等待发生在工具
      超时保护之外**：人的思考时间不再烧 tool_timeout，续段执行照常享受
      超时 + 瞬态重试
- [x] 评测：脚本化审批策略门（ScriptedPolicyGate：低风险额度内秒批、超额度/
      资金操作秒拒）+ 策略集 3 条（app-01/02 放行路径钉住执行、app-03 拒绝
      路径防假装）独立 live 模块 + 标注标准 §7.1——**首跑即抓到第 1 周真实
      缺陷**：桥接层调用 handler 固定 include_writes=False，写工具"能发现
      不能调用"（AutoDeny 评测到不了执行段所以无感），已修 + 桥接端到端
      放行回归测试
- 真实链路预跑：策略集 3/3 + AutoDeny 写集回归 3/3（reports/evals/
  20261005-124642*.md、20261005-124742*.md，合计成本 ≈¥0.005）
- 产出：agent_core 挂起协议（ApprovalSuspended + StreamApprovalGate）+ 事件
  流三端贯通 + 评测策略集；全仓离线单测 152 项全绿，tsc 通过

### 第 3 周 — 写路径错误自愈 + 幂等
- [ ] 写路径错误自愈观察开档（error-recovery-log 写路径篇）：invalid_transition /
      insufficient_stock 的 hint 能否让模型改道（先查状态再操作）
- [x] 幂等：四种 mutation 均支持 client_token——重复提交返回原结果，不二次变更；
      BEGIN IMMEDIATE 保护并发库存与订单号，结果记录与业务同事务（ADR-0007）
      create_order 幂等键（client_token）——重复提交返回原单而非二次建单
      （mcp_erp 包注释里挂账的"幂等键用武之地"兑现）
- [x] 现有 app-01/02 放行 case 加成功结果/执行次数/准确状态检查，app-03 与
      WRITE_CASES 检查拒绝零业务变更；每条独立临时库，测试防假成功和重复写入。
- [x] 审批入口与生命周期：真实工具 risk 选择提示词、取消/断连/并行 waiter 清理、
      未完成会话历史回退、前端提交失败可重试；AnyIO SSE 取消回归。
- [x] 写路径错误恢复工程演示（库存不足 → get_stock 复核）；该演示脚本化模型，
      真实模型对 invalid_transition / insufficient_stock 的恢复观察仍未完成。

### 第 4 周 — M4 收口
- [ ] 真实链路验收：审批前（AutoDeny）与审批后（人工批准）两段完整数字
- [x] 统一 35 条基线入口与 CI：共享预算、各自工具面/门、版本/范围哈希、
      每条 checkpoint；定点与完整报告明确区分，保留失败及缺失计量。
- [x] 文章素材沉淀：[可靠性复盘](write-reliability.md)，尚未发布到 VIE。
- [x] 本轮离线验收：179 项 pytest 通过、35 条 eval 默认排除；ruff / 前端测试 /
      生产构建通过，浏览器报价/批准/拒绝/恢复链路通过，控制台无 error/warn。
- [x] ADR-0007 / 工具卡 / 标注标准同步，CI 加入前端测试与构建。
- [ ] M4 真实模型行为全部达到验收线：以本次统一报告为准，不能用定点预跑拼成功率。
- 本轮完整报告 `reports/evals/20261005-142553-dcc830.md`：34/35，写拒绝与
  脚本化审批 6/6，≈¥0.0308 / 1255s；edge-08 保留为状态澄清/判分边界问题。

## 6c. 持久化恢复阶段（2026-10-06，开发与验收分开）

承接 M4 尚未完成的真实模型观察与真人审批验收，再推进任务/审批持久化、
稳定调用 token、提交结果对账、断连与重启恢复。
详细范围、恢复语义和验收矩阵见 [下一阶段总体规划](../tasks/plan.md)，
12 项可交接任务及 4 个检查点见 [任务清单](../tasks/todo.md)。

T05/T06 已补强，T07–T10 持久化审批、快照/游标、后台执行、前端重连和四工具
硬终止回归已接入。2026-10-06 真实模型完整基线执行 35 条、32 条通过，低于
34 条门槛；独立 `write-errors` 执行 2 条但均未触发写错误码，模型先做只读核验并
结束。分别见[完整报告](../reports/evals/20261006-143939-c58ccc.md)与
[写错误报告](../reports/evals/20261006-150323-754013.md)。估算费用合计 ¥0.0384。
本次未执行 B02 真人审批；T09 断网/强制进程浏览器操作、T11 真人审批与 C3/C4
整体验收保留待办。工程证据与复现入口见[恢复验收交接](plans/恢复验收交接.md)，
真实审批步骤见[操作手册](plans/真实验收操作手册.md)。历史 34/35 报告不回写，
M4 第 3/4 周的真实观察未勾选项保持原状。

本节不变更第 4 节范围冻结；仍为本机单用户、手写 loop、
SQLite 与 20 个工具。身份认证、公开部署以及 LangGraph/PostgreSQL 迁移
作为后续决策，不因新增规划视为已接受或已实施。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| Python 生态不熟（asyncio、类型标注、uv 工具链） | 第 1 周只做地基不赶进度；AI 编程工具辅助提效；asyncio 模式沉淀成第一篇 ADR 素材 |
| 评测集质量低、case 凑数 | 先写人工标注标准再写 case；每周固定补 5 条；judge 与人工一致率 <85% 就停下校准 |
| API 成本失控 | 设月度预算上限；CI 回归只跑便宜模型；trace 记录单任务成本 |
| 半途需求膨胀 | 第 4 节范围冻结；变更走 ADR |
| 时间不足（在职） | 每周 ≥6h 底线投入；M6 起现职功能与本项目互补而非并行竞争 |
| 模型 API 变动 | LiteLLM 网关层隔离供应商差异，env 一键切换 |

## 8. 已定项与剩余待定

- [x] 项目名：**Erpilot**（ERP + pilot；"掌柜"保留为产品隐喻——掌柜打理日常买卖，动账动货必须请示东家，即 HITL 审批）
- [x] 模型主力：GLM-5.3 Flash（智谱，OpenAI 兼容端点起步；评测阶段横向对比 DeepSeek / Qwen）
- [x] 仓库已初始化：`E:\workspace\erpilot`（uv workspace + 四包骨架 + ADR-0001 + CI），pytest / ruff / API 冒烟全部通过
- [x] M1 第 1 周完成：LLM client（流式 + usage + 成本估算）+ demo 入口 + 10 项单测通过 + 真实链路冒烟通过（2026-09-29，经 flashcoding.ai 中转端点，单次成本 ≈¥0.00015，按现价目表折算）
- [x] M1 第 2 周完成：AgentLoop 工具循环（tools 协议 + max_steps/超时防护 + structured 输出）+ 26 项单测 + 真实链路验收（2026-09-29，单工具任务 2 步正常结束）
- [x] M1 第 3 周完成：多步任务 + 并行工具调用 + 错误回填策略 v1（结构化错误 + 瞬态重试）+ 上下文压缩 v1 + 34 项单测 + 真实链路验收（2026-09-29，3 步任务 ≈¥0.0003，第 2 步模型自发并行调用两工具）
- [x] M1 第 4 周完成（代码侧）：本地 JSONL trace + `erpilot` CLI（chat/replay）+ FastAPI SSE 链路 + React 最小流式页 + ADR-0002/0003 + 文章文稿 + 44 项单测通过（2026-09-30）。真实链路当日两次验收尝试均遇上游故障（APIError / 502 upstream_error），均被 trace 的 run_error 完整留痕——异常留痕路径实战验证通过；成功任务的完整数字待端点恢复后补跑
- [x] M1–2 里程碑收口、M3 提前启动（2026-09-30）：《手写 Agent Loop》发布 VIE；M3 第 1 周（erp_store 领域模型 + 种子数据）当日完成
- [x] M3 第 2 周完成：FastMCP Server（10 只读工具）+ MCP→agent 桥（schema 动态转 Pydantic）+ CLI 迁至 apps/cli + ChatService 工具注入 + 65 项单测（2026-09-30）。真实链路验收：erpilot chat 经 MCP 查真数据，3 步正常结束（5901 tok ≈¥0.0007），补齐 M1 验收线的真实数字；模型对"订单 123 不存在"结构化错误自发改道——错误自愈首次实证
- [x] M3 第 3 周完成：错误契约 v1（code/message/hint）+ 工具扩到 16 个（新增 get_customer_purchases 客户购买聚合、按 SKU 反查订单、批量比价、库存估值、逐日销量）+ 工具卡 16 张（文档-代码同步测试）+ 错误自愈记录开档（docs/error-recovery-log.md #1）+ 72 项单测（2026-09-30）
- [x] M3 第 4 周完成（M3 里程碑收口）：人工标注标准 + 24 条四类 case + pytest 自建 runner（ADR-0004）+ 预算熔断 + Langfuse 双写（本地 JSONL 降级为兜底 sink）+ CI 评测 job + 首份成功率报告（reports/evals/，详见 §6 第 4 周）（2026-10-05）
- [x] 评测常态运维第 5 周批次（2026-10-05）：runner 瞬态重试 + 系统提示词加固（adv-05 复测通过）+ 补 5 条 case（评测集 24→29 条，详见 §6）
- [x] M4 第 1 周完成（2026-10-05 当日）：ADR-0005（写操作分层 + 风险分级 + 审批门内建）+ erp_store 写 API ×4（快照价/库存同事务/状态机）+ agent_core 审批门原型（ApprovalGate + guarded）+ mcp_erp 写工具 ×4（默认关闭，writes=True 必须 gate）+ 写操作评测 3 条真实链路预跑通过（详见 §6b）
- [x] M4 第 2 周完成（2026-10-05 当日）：ADR-0006（挂起式审批事件协议）+ ApprovalPending/Resolved 事件三端同源（trace/SSE/前端卡片）+ CLI 终端审批（--writes）+ API 审批端点 + 脚本化审批策略集 3 条（首跑抓到桥接写工具不可调用的第 1 周缺陷，已修）+ 真实链路预跑 6/6（详见 §6b）
- [ ] assistant-ui 还是 CopilotKit（M6 前端成型时定；M1–M5 先手写最小 React UI，理解协议层）
- [ ] Langfuse 真实端到端联调（sink 已就绪并有 fake 单测；待自托管部署后填 keys 验证）

## 9. 参考资料

- The New Stack：How to build production-ready AI agents with RAG and FastAPI — https://thenewstack.io/how-to-build-production-ready-ai-agents-with-rag-and-fastapi
- CopilotKit 文档（AG-UI、generative UI、MCP Apps）— https://docs.copilotkit.ai
- ruanyf/weekly 招聘汇总（2026-09）— https://github.com/ruanyf/weekly/issues/11434
- 7 Best AI Agent Frameworks Compared（2026-06，LangGraph / CrewAI / AutoGen / Google ADK / OpenAI Agents SDK 对比）
