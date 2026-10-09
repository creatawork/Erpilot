# RAG 真实验收（已通过）

日期：2026-10-09。目标是构建真实政策向量索引、校准检索阈值，并验证命中回答、语料外拒答、来源引用和检索内容注入行为。

## 阿里云连接与索引构建

| 步骤 | 结果 | 证据 |
|---|---|---|
| 本地配置 | 已完成 | `.env` 使用 `DASHSCOPE_API_KEY`，向量端点为 DashScope OpenAI 兼容接口，模型为 `text-embedding-v4`；凭据未写入本报告或代码 |
| 最小真实请求 | 成功 | 阿里云返回 1024 维向量，单条样例用量 11 tokens |
| 首次批量建索引 | 首次失败，原因已修复 | `text-embedding-v4` 单次最多接收 10 条；原嵌入器把 18 个片段一次提交。现已在嵌入器中按 10 条分批 |
| 真实向量索引 | 成功 | 已嵌入并写入 18 个政策片段：`packages/rag/src/rag/policy_index.json` |
| RAG 离线测试 | 通过 | `uv run --package rag pytest packages/rag/tests -q`：25 passed；新增批量分组回归测试 |

## 之前的端点尝试

此前默认智谱 `embedding-3` 请求返回 HTTP 401；FlashCoding 当前聊天端点的 embeddings 请求返回 HTTP 404，平台提示不支持 Embeddings API。失败响应没有返回 usage，无法据此确认是否计费。

## 完整真实验收结果

完整链路已使用 DashScope `text-embedding-v4` 与 FlashCoding `glm-5.3-flash` 通过验收。阈值校准覆盖 6 条语料内正例和 6 条语料外负例，阈值定为 `0.66`；正例最低分 `0.7218`，负例最高分 `0.6103`。5 个真实 MCP + LLM 场景全部通过：退货规则、数量折扣、发货后取消与库存、语料外拒答、检索内容注入。详见[完整验收报告](20261009-150829-full-acceptance.md)及同名 JSON 和 traces。

`search_policy` 的分数阈值已从占位 `0.35` 调整为 `0.66`。索引文件是本地验收产物。报告不含 API 凭据。DashScope 使用独立的 Embedding 配置，与聊天模型端点分离。
