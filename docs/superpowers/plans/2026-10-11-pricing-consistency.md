# B1 计价一致 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 报价与新订单在同现价、同商品和同数量下采用同一折扣及金额口径。

**Architecture:** 在 erp_store 新建标准库十进制定价模块，查询与事务写入复用。保留历史成交价、幂等结果和外部数值字段。

**Tech Stack:** Python 3.12、Decimal、SQLAlchemy / SQLite、FastMCP、pytest。

**Spec:** `docs/superpowers/specs/2026-10-11-pricing-consistency-design.md`

## Global Constraints

- Python >=3.12；不新增第三方依赖。
- `agent_core` 保持零业务依赖；定价函数只在 `erp_store`。
- 保持 MCP 工具参数及返回金额字段兼容；保留 `repository.QUOTE_TIERS` 的导入入口。
- 历史单据和已有幂等记录不重算。

## Review Focus

- 合并同 SKU 后恰好跨过折扣门槛，应按合并数量计价。
- 不同 SKU 数量合计跨门槛，不能共享折扣。
- 折后单价尾数为半分，须按 HALF_UP 而不是二进制 round 舍入。
- 商品涨价后旧 token 重放，须返回首次成交价并只扣一次库存。
- 多行建单有一行库存不足，须完整回滚。

## Task 1：固定场景回归与共享计价

**Files:** 新建 `packages/erp_store/tests/test_pricing_consistency.py`、`packages/erp_store/src/erp_store/pricing.py`；修改 `repository.py`、`mutations.py`、`models.py`。

**Interfaces:** `price_for_quantity(price: float, quantity: int) -> tuple[float, float, float]` 返回 discount/unit_price/total；`PricingError(ValueError)` 表达无有效成交价。订单模型保留现有汇总函数，维护旧单据与幂等结果兼容。

- [x] 创建固定商品 100 元、库存 1000 的临时库，参数化数量 1/9/10/49/50/199/200/201，报价与订单均断言手算折后金额。
- [x] 增加 0.75 元 × 10 件的半分舍入用例，断言单价 0.74、总价 7.40；增加两 SKU 各 5 件不打折和同 SKU 6+4 件按 10 件打折。
- [x] 运行新测试，确认旧代码因订单无折扣或舍入错误失败。
- [x] 共享定价使用 `Decimal(str(price)) * Decimal(str(discount))` 和 `quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`；查询与事务内建单接入。订单模型保持历史汇总规则，新单汇总读取已舍入成交价。
- [x] 运行 erp_store 测试，确认新回归与现有查询、写入、并发、幂等测试通过。

## Task 2：事务、历史与工具边界

**Files:** 扩充 `test_pricing_consistency.py`；扩充 `packages/mcp_erp/tests/test_mcp_erp.py`。

**Interfaces:** 复用现有 `ErpMutations.create_order` / `cancel_order` / `lookup_result` 和 MCP `compute_quote` / `create_order`，不改变签名。

- [x] 在真实临时库建折扣单，更新商品现价并重启 mutation 实例；用原 token 重放，断言返回同一订单、成交单价未改、库存只扣一次。
- [x] 验证折扣单取消后库存回补；多 SKU 任一库存不足时客户订单数为 0，所有库存均不变。
- [x] 使用真实 MCP Client 创建 10 件订单并报价，断言数值与手算相符，避免只比较两个可能共同出错的结果。
- [x] 运行 erp_store、mcp_erp 及 RAG 语料测试。

## Task 3：政策、入口与验收记录

**Files:** 修改 `packages/rag/src/rag/policies/pricing-policy.md`、`packages/mcp_erp/src/mcp_erp/server.py`、`docs/tool-cards.md`、`README.md`、`tasks/todo.md`。

**Interfaces:** 保留旧阶段完成记录，新增功能路线入口；政策说明精确舍入、成交快照及旧报价参考性。

**附带修正：** 默认政策装配比较索引片段与当前语料，源文件修改、添加、删除时返回 `policy_unavailable`，避免旧口径继续生效。对应测试先在旧实现下 3 failed / 1 passed，再接入检查。

- [x] 同步商品价格、折扣、新单成交价及金额舍入口径；确认政策索引需要重建时显式记录，不用旧索引宣称新政策已生效。
- [x] 全仓 `uv run pytest`、`uv run ruff check .`；离线测试不调用真实模型；索引重建单独调用嵌入服务。
- [x] 检查本次提交 diff 只包含本次修改，更新本计划执行记录和里程碑状态。
- [x] 在测试通过后仅提交本次修改，不混入此前未提交的评测、安全报告和文档内容。

## 执行记录

2026-10-11：用户已要求直接开始开发，本轮采用当前会话执行，不再次请求启动许可。当前工作区存在上阶段未提交内容，保留原样；在 `codex/business-flow-pricing` 分支实施，不创建丢失这些工作内容的新 checkout。本计划限定 B1；B2–B6 见路线文档，各阶段另立设计和计划。

执行记录：全仓基线 394 passed / 21 skipped / 47 deselected。定价回归首次运行 12 failed / 3 passed，复现订单未折扣与半分舍入问题；接入共享计算后 erp_store 53 passed。旧种子报价测试依赖二进制 round，现按已批准的 HALF_UP 契约更新期望，保留档位验证。

Ruling：同步政策文件必须同时重建索引，否则现有 RAG 会因过期索引拒绝服务；本轮额外调用现有嵌入服务构建索引，不运行真实聊天模型评测。原构建命令未加载 `.env`，首次失败于读取配置；改用 uv 原生 `--env-file .env`，无须引入重复的配置解析器或运行时依赖。18 个片段已成功重建。

阶段验证：首轮 erp_store + mcp_erp + rag 共 115 passed；审查修正后 118 passed。最终全仓 `uv run pytest`：417 passed / 21 skipped / 47 deselected，31.36s；`uv run ruff check .` 通过；`git diff --check` 通过。独立 reviewer 复核两项修正，未发现新的可操作问题。完整记录见 [B1 验收](../../../reports/acceptance/2026-10-11-pricing-consistency.md)。

审查修正记录：独立 reviewer 找到旧订单半分总额重算和零成交价错误未转译两项问题，均先增加回归确认失败。Ruling：本轮保留订单模型原汇总算法，只统一报价与新单成交价，避免为计价版本新增数据库迁移；删除未再需要的总额辅助函数。零成交价统一拒绝，返回业务错误且不占用幂等键。后续全库 Decimal 迁移需显式历史计价版本设计。
