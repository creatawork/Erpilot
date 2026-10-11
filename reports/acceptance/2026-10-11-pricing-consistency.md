# B1 计价一致验收

日期：2026-10-11。分支：`codex/business-flow-pricing`，实施基于 `3f413f6`。范围与完成状态以 [设计](../../docs/superpowers/specs/2026-10-11-pricing-consistency-design.md) 和 [计划](../../docs/superpowers/plans/2026-10-11-pricing-consistency.md) 为准。

## 已交付

- 报价和新单共用 `erp_store.pricing.price_for_quantity`；同 SKU 合并后命中折扣，不跨 SKU 累加。
- 折后单价采用十进制 HALF_UP 到两位，按舍入后单价计算行金额。报价 0.75 元 × 10 件的成交单价为 0.74、总价 7.40。
- 建单计价在现有写事务内执行；库存不足、商品状态错误和无法形成正数成交价时不产生部分提交。
- 历史订单汇总算法保留；原 token 重放保持首次成交价格、金额和订单号，不重复扣库存。
- 更新价格政策及 MCP 描述；默认检索校验索引内容与当前源文档一致，旧索引返回 `policy_unavailable`。
- 使用 `uv run --env-file .env --package rag python -m rag build` 成功重建 18 个政策片段，原命令漏加载 `.env` 的问题通过 uv 原生参数及文档/错误提示修正。

## 验证证据

| 验证 | 结果 |
|---|---|
| 修改前全仓离线基线 | 394 passed / 21 skipped / 47 deselected |
| 新定价回归在旧代码下 | 12 failed / 3 passed，失败原因是订单未折扣或舍入口径不同 |
| 旧索引检查在旧代码下 | 3 failed / 1 passed，旧文档内容仍被检索返回 |
| 审查发现的历史半分金额与零成交价回归 | 修正前分别失败；修正后通过 |
| 最终定价、MCP、RAG 测试 | 118 passed，13.40s |
| 最终全仓 `uv run pytest` | 417 passed / 21 skipped / 47 deselected，31.36s |
| 全仓 `uv run ruff check .` | All checks passed |
| `git diff --check` | 通过；只有 Windows 换行转换提示 |
| 独立代码审查与修正复核 | 两项问题均修复，无新增可操作发现 |

测试使用真实临时 SQLite 与 MCP 协议，固定商品和手算金额断言；未调用真实聊天模型。真实模型评测由默认 marker 排除，21 个跳过测试不计为通过。本报告不宣称重新通过 PostgreSQL 硬崩溃矩阵或浏览器验收；本轮未变更图状态、审批协议或前端。

嵌入构建为真实外部调用；18 个片段写入既有索引文件。构建器当前未输出 token usage，无法给出实际用量或账单成本，本报告不推算成实测值。

## 边界与下一步

B1 完成的是计价基础。当前报价不锁价、不预留库存；完整「按已批准报价建单」需要 B2 的订单草稿、版本绑定与执行复核。API/数据库金额字段及历史汇总保持兼容，不是全库 Decimal 迁移。政策检索阈值仍为既有 0.66；本轮未重新校准真实模型 RAG 成功率。

下一里程碑 B2：先落地会话归属的多商品草稿、确定性价格明细与版本，再让审批绑定该版本；修改使旧审批失效，执行条件变化需重新确认。后续 B3–B6 见 [功能路线](../../docs/business-roadmap.md)。
