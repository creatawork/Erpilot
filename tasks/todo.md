# 当前阶段：批次 2 · RAG 政策问答（批次 1 待 key 收尾）

更新日期：2026-10-09。目标：在现有 loop 上接入**只读检索**的政策问答（价格/折扣/退换货），回答基于检索片段并给出来源，语料外问题如实拒答。优先 JD 硬指标（多模型 + RAG），深度优先，不设固定发布日期。

- 下一阶段总体规划：[plan-next-phase.md](plan-next-phase.md)（含批次 3–5：评测叙事、部署 demo、文章）。
- 上一轮「基线验收与失败归因」存档：[2026-10-08 基线存档](todo-2026-10-08-baseline-archive.md)。
- 历史恢复协议规划：[plan.md](plan.md)；长期路线：[启动计划](../docs/agent-project-plan.md)。

当前执行边界：本机、可信单用户；保持 `agent_core` 零业务依赖；RAG 以只读检索工具接入，不新增业务写能力。

## 批次 2 待办（C2：检索接入 loop；grounded 回答 / 语料外拒答 / 引用来源）

- [x] R01 / P0：新建业务无关检索引擎包 `packages/rag`——`chunk`（标题分块）+ `embed`（`Embedder` 协议 + 余弦）+ `index`（`PolicyIndex` 余弦 top-k + 阈值拒答 + JSON 持久化）。14 条离线单测（fake embedder，不烧 token）。
- [x] R02 / P0：政策语料 3 份（价格/报价、订单/取消、退换货）固定入库 `packages/rag/.../policies/*.md`，折扣档位/状态机与 `erp_store` 口径一致；`corpus.load_corpus` 加载分块；真实嵌入器 `OpenAIEmbedder`（默认智谱 embedding-3，`EMBED_*` 可覆盖）+ 构建入口 `python -m rag build`。10 条离线单测（含语料规则校验、嵌入器 mock）。
- [x] R03 / P0：`search_policy` 只读 MCP 工具接入 `mcp_erp`（返回片段 + source/title + score；空命中返回拒答提示；索引未built 返回 `policy_unavailable`）；`policy_resolver` 可注入贯穿 `create_server`/`bridge`；系统提示词补「先检索政策、依据片段作答注明来源、无命中如实拒答不臆造」。5 条测试（server 级 + 桥接端到端，fake embedder）。只读工具 16→17、写面 20→21，工具卡同步。
- [ ] R04 / P0：评测新增 RAG 子集——命中 grounded 回答、语料外拒答、引用正确来源、注入片段不改写意图；单独统计。**依赖 R05 阈值校准**（否则可能因阈值而非模型失败）。
- [ ] R05 / P1：`python -m rag build` 构建真实索引（消耗嵌入 token），用真实嵌入校准 `POLICY_SCORE_THRESHOLD`（现为占位 0.35）正/负例；存储/嵌入选型取舍写入报告或 README；留一次真实检索链路证据。

说明：R01–R03 纯离线完成并测试覆盖；R04/R05 需构建索引（嵌入 key + token）与真实链路，待 key。`search_policy` 在索引未构建时对模型返回 `policy_unavailable`（优雅降级），不影响现有链路启动。

## 批次 1 剩余（待各供应商真实 key，消耗 token）

- [ ] M04 / P0：跨 GLM-5.3 Flash / DeepSeek / Qwen 跑现有基线，`python -m evals.compare` 产出对照表。
- [ ] M05 / P1：对比结论写入报告 + README 多模型对照入口；标注端点差异为真实观察。
- 遗留：`prices.py` 补 DeepSeek/Qwen 实测价目（M04 跑前填）。

已交付（批次 1）：M02 provider profile 层（zhipu/deepseek/qwen，默认兼容，7 单测）；M03 runner 已原生记录模型维度 + `evals/compare.py` 对照聚合器（7 单测）。

## 验证入口

- `uv run pytest`、`uv run ruff check .`
- 切供应商：`.env` 设 `LLM_PROVIDER=deepseek`（填 `DEEPSEEK_API_KEY`）或 `=qwen`（填 `DASHSCOPE_API_KEY`）。
- 跨模型对照：`uv run python -m evals.compare`。

判定规则：报告产出与问题归因可以完成；运行门槛有失败时如实标未通过。RAG 语料外臆造答案、跨模型拼成虚构统一成功率均零容忍。
