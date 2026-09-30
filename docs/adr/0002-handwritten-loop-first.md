# ADR-0002：M1 先手写 agent loop，LangGraph 推迟到 M6

- 状态：已接受
- 日期：2026-09-30

## 背景

路线图（见 ADR-0001）已定 M6 起用 LangGraph 承载编排。M1 面临的选择是：直接从 LangGraph 起步，还是先用几百行代码手写一遍 agent loop（流式、工具调用、防死循环、超时、错误回填、上下文压缩），再在 M6 迁移。

约束：这是求职作品集项目，面试中"agent 原理"是必考题；同时项目要按期产出业务价值（ERP 智能体），不能无限期停在造轮子。

## 决策

M1（第 1–4 周）手写完整 agent loop，不引入任何编排框架；M6 起重构为 LangGraph（Postgres checkpointer + interrupt 实现 HITL）。

手写范围冻结为四件事：

1. 事件模型：`stream_chat` 产出 `TextDelta / ToolCall / StreamEnd`，loop 在此之上产出 `StepStarted / StepEnd / ToolCallStarted / ToolCallFinished / LoopEnd`——恰好是 trace、CLI、SSE、前端共同消费的一层协议
2. 防护：max_steps 防死循环、单工具 asyncio.wait_for 超时、异常永不外抛而是结构化回填
3. 策略：瞬态错误重试（ToolRetryPolicy）、上下文压缩（ContextPolicy）、并行工具调用（as_completed）
4. 可观测：trace.py 旁观事件流落盘 JSONL，loop 本身对"被记录"无感知

## 理由

- **面试原理题**：模型如何决定调用工具、tool 消息为何必须与 assistant tool_calls 成对、上下文为何要按轮裁剪——亲手实现过一遍才有第一手答案，这是框架给不了的
- **事件协议自主权**：SSE 前端、trace 格式、成本计量的字段形状由自己定义，M6 换 LangGraph 时被替换的只是 loop 内核，这层协议和四个消费方不动
- **面积可控**：loop.py + context.py + llm.py 合计约 600 行、44 项单测，远未到"重新发明 LangGraph"的规模；LangGraph 真正解决的（checkpoint 持久化、interrupt 人工介入、图编排）恰好都在 M6 之后的 HITL 阶段才需要
- **迁移风险前置消化**：手写阶段积累的事件流、错误回填、压缩策略直接定义了 LangGraph 重构的验收标准

## 备选与放弃原因

- 直接 LangGraph 起步：HITL/checkpoint 开箱即用，但 M1 用不上这些能力，且跳过原理层，面试与后续调优都吃亏
- OpenAI Agents SDK：更轻，但绑定 OpenAI 生态；本项目模型路由要走 LiteLLM（ADR-0001），且它同样掩盖原理
- 永远手写不迁移：HITL 的审批/恢复/checkpoint 是工业级需求，自研成本远高于学习成本，不值得

## 后果

- M6 重构有一次真实迁移成本（事件模型对齐 LangGraph 的 astream 事件、checkpointer 接管会话历史），已计入里程碑
- M1–M5 期间多会话并发、断点续跑、持久化需要自己兜底（当前是内存会话 + 本地 trace，够 demo 不够生产）
- 换框架的窗口在 M6 前始终敞开：事件协议层保持稳定，迁移面收敛在 loop 内核
