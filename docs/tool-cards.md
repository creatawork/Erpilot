# MCP 工具卡（erpilot-erp · 16 个只读工具 + 4 个写工具）

> M3 第 3 周 · 工具设计精研（上）；M4 第 1 周起增补写操作（ADR-0005）。
> 每个工具一张卡：用途 / 参数 / 返回 / 错误。
> 本文档与 server 工具集有同步测试把关（`test_tool_cards_doc_matches_server_tools`）——
> 新增或删除工具必须同步改这里，否则测试失败。
> 写工具仅在 `include_writes=True` 时注册（默认关闭）；agent 侧执行受审批门
> （`agent_core.approval`）拦截，风险等级 `single_confirm` / `batch_confirm`
> 由 bridge 标注——审批拒绝时返回 `{"approval": "denied", ...}`，
> 模型必须如实说明未执行。

## 错误契约 v1

所有业务错误统一为：

```json
{"error": {"code": "not_found", "message": "订单不存在：SO-xxx", "hint": "可用 list_orders 浏览现有订单"}}
```

- `code`：`not_found`（查无此物）/ `invalid_argument`（参数不合法）——模型据此分类处理
- `message`：发生了什么，含收到的原始值
- `hint`：**可操作的下一步**——用哪个工具、传什么参数，或向用户要什么信息
- 业务校验在工具体内做并返回此结构，而不是靠 schema 约束抛协议异常——
  协议异常到模型手里只剩一句 pydantic 报错，没有自愈线索
- 空**列表**结果不是错误（`{"total": 0, "items": []}`），模型自行向用户说明

## 订单

### get_order
- 用途：按订单号查询订单（状态、客户、明细与金额）
- 参数：`order_id`（必填，格式 SO+日期+序号，如 SO20260301-0001）
- 返回：订单对象；`items[].unit_price` 是**下单快照价**（可能与现价不同），`total_amount` 按快照价汇总
- 错误：`not_found` → hint 建议用 `list_orders` 浏览或请用户提供完整单号

### list_orders
- 用途：按时间倒序列订单（**逐单浏览**用；按客户统计"买了什么/花了多少"请用 `get_customer_purchases`，一次聚合结果小得多）
- 参数：`status`（可选，待付款/待发货/已发货/已签收/已取消/已退款）、`customer`（可选，精确匹配）、`limit`（1~50，默认 20）、`offset`、`detail`（默认 False）
- 返回：`{"total", "items", "note"}`——**默认不含明细行**（订单头：单号/客户/状态/时间/件数/金额），`detail=true` 才带明细且 limit 自动收紧到 20
- 错误：`invalid_argument`（状态取值非法时 hint 列出全部可选值；limit/offset 越界）

### get_orders_by_sku
- 用途：反查某 SKU 进了哪些订单（含全部状态）——"这个商品都卖给谁了"
- 参数：`sku`（必填）、`limit`（1~50，默认 20）、`detail`（默认 False，同 list_orders 瘦身）
- 返回：`{"total", "items", "note"}`
- 错误：total 为 0 表示该 SKU 无订单（合法结果，非错误）

### get_customer_purchases
- 用途：**聚合某客户的购物汇总**——买过什么（按商品聚合件数/金额）、各状态多少单多少钱、总花费。回答"某人买了些什么/总共多少钱/哪些已发货哪些未付款"用本工具，一次调用代替逐单翻页
- 参数：`customer`（必填，精确匹配；不确定写法先 list_orders 确认）
- 返回：`{"customer", "order_count", "total_amount"（有效口径=待发货/已发货/已签收）, "total_amount_all"（含取消/退款/待付款）, "by_status": [{status, order_count, total_amount}], "items": [{sku, name, total_quantity, total_amount}]（按金额降序，最多 40 行）}`
- 错误：`invalid_argument`（客户名为空）；`not_found`（该客户无订单 → hint 用 list_orders 确认写法）

## 商品

### get_product
- 用途：按 SKU 查商品（名称、品类、现价、在售状态）
- 参数：`sku`（必填）
- 返回：商品对象；`status` 为"已下架"时报价类工具会拒绝
- 错误：`not_found` → hint 建议用 `search_products` 找 SKU

### search_products
- 用途：按名称/品类关键词搜商品
- 参数：`keyword`（必填，非空）、`limit`（1~100，默认 20）
- 返回：`{"total", "items"}`（商品对象）
- 错误：`invalid_argument`（关键词为空，hint 给示例词）

### list_products
- 用途：不带关键词的商品浏览入口，按状态/品类组合过滤
- 参数：`status`（可选，在售/已下架）、`category`（可选，精确匹配，可先调 `list_categories`）、`limit`、`offset`
- 返回：`{"total", "items"}`
- 错误：`invalid_argument`（状态取值非法）

## 库存与报价

### get_stock
- 用途：按 SKU 查当前库存数量与仓库
- 参数：`sku`（必填）
- 返回：`{"sku", "quantity", "warehouse"}`；quantity 为 0 表示缺货（合法状态）
- 错误：`not_found`（SKU 不存在 → hint 用 search_products 确认）

### compute_quote
- 用途：按现价 × 数量梯度折扣报价（≥10 件 98 折 / ≥50 件 95 折 / ≥200 件 9 折）
- 参数：`sku`（必填）、`quantity`（必填，≥1）
- 返回：`{"sku", "name", "unit_price", "quantity", "discount", "total", "stock_quantity"}`——
  `stock_quantity` 为 0 时报价仅参考（工具层应向用户说明缺货）
- 错误：`invalid_argument`（数量 < 1）；`not_found`（商品不存在或**已下架**，hint 建议换在售商品）

### compare_quotes
- 用途：多商品同数量批量比价，结果按总价升序
- 参数：`skus`（必填，2~10 个，自动去重保序）、`quantity`（必填，≥1）
- 返回：`{"quantity", "quotes": [...], "unavailable": [...]}`——不可报价（不存在/已下架）的 SKU
  进 `unavailable` 而不是整体报错
- 错误：`invalid_argument`（skus 数量不在 2~10、quantity < 1）

### list_low_stock
- 用途：低库存商品清单（按库存升序）——盘库存、补货建议
- 参数：`threshold`（默认 10）、`limit`（1~100，默认 20）
- 返回：`{"returned", "items": [{"sku", "name", "category", "price", "quantity", "warehouse"}]}`；returned 是本次返回条数，非全量计数
- 错误：`invalid_argument`（threshold 不在 0~10000 等）

## 运营视图

### sales_summary
- 用途：近 N 天销量汇总——**有效口径**：待发货 / 已发货 / 已签收（取消、退款、待付款不计）
- 参数：`days`（1~365，默认 30）
- 返回：`{"days", "order_count", "total_amount"}`（金额按快照价）
- 错误：`invalid_argument`（days 越界）

### top_products
- 用途：近 N 天畅销榜（按销量件数降序），只统计有效订单
- 参数：`days`（1~365，默认 30）、`limit`（1~50，默认 10）
- 返回：`{"returned", "items": [{"sku", "name", "category", "total_quantity", "order_count", "total_amount"}]}`
- 错误：`invalid_argument`（days/limit 越界）

### daily_sales
- 用途：近 N 天逐日销量点——看趋势、找异常日
- 参数：`days`（1~90，默认 14）
- 返回：`{"returned", "items": [{"date", "order_count", "total_amount"}]}`，items 按日期升序
- 错误：`invalid_argument`（days 越界）

### stock_valuation
- 用途：库存估值——按品类的库存数量与金额（现价口径），掌柜算家底
- 参数：无
- 返回：`{"grand_total_value", "categories": [{"category", "sku_count", "total_quantity", "total_value"}]}`
- 错误：无（空库返回 total 0）

### list_categories
- 用途：列出全部品类与在售商品数——探索库存时的第一步
- 参数：无
- 返回：`{"total", "items": [{"category", "product_count"}]}`
- 错误：无

## 写操作（include_writes=True 才注册；ADR-0005）

> 领域校验在 `erp_store.mutations`，错误契约 v1 在写路径新增两个 code：
> `invalid_transition`（状态机非法迁移 / 空操作）与 `insufficient_stock`。
> 写工具的执行永远先过审批门——拒绝时模型拿到的是"未执行"，不是错误。

四个写工具均接受可选 `client_token`（1–128 个字符）。成功结果与业务变更
同事务保存：同键同规范化参数返回原结果，重启后仍有效；同键不同参数返回
`idempotency_conflict`。键的范围覆盖四个写工具，新操作须使用新键。
失败事务不占用键。bridge 在未传键时为单次工具执行生成键并在自动重试中
复用；重新发起模型调用会产生新键，调用方跨请求重试应显式保留键。
直接调用 MCP Server 仅执行领域校验；审批门由 agent bridge 装配。

### create_order
- 用途：创建订单（**single_confirm**）——快照价取现价，校验在售与库存，扣库存与建单同事务
- 参数：`customer`（必填，全名）、`items`（必填，`[{sku, quantity}]`，同 SKU 自动合并）、`note`（可选）
- 返回：订单对象（状态「待付款」，金额按下单快照价）+ `result_note`
- 错误：`invalid_argument`（客户名空 / 数量 < 1）；`not_found`（SKU 不存在，hint 用 search_products）；
  `invalid_transition`（商品已下架不可下单）；`insufficient_stock`（库存不足，报需量与现量）

### cancel_order
- 用途：取消订单（**single_confirm**）——仅待付款/待发货可取消，取消后回补库存
- 参数：`order_id`（必填）
- 返回：订单对象（状态「已取消」）+ `result_note`
- 错误：`not_found`（订单不存在，hint 用 list_orders）；`invalid_transition`
  （状态不允许取消——已发货/已签收走退款流程，工具面未开放）

### adjust_stock
- 用途：库存增减（**batch_confirm**）——正数入库、负数出库
- 参数：`sku`（必填）、`delta`（必填，≠0）
- 返回：`{"sku", "quantity", "note"}`——quantity 为调整后的当前库存
- 错误：`invalid_argument`（delta = 0）；`not_found`（SKU 不存在）；
  `insufficient_stock`（减到负数，报当前库存）

### set_product_status
- 用途：商品上架/下架（**batch_confirm**）
- 参数：`sku`（必填）、`status`（必填，在售/已下架）
- 返回：`{"sku", "status", "note"}`
- 错误：`invalid_argument`（状态取值非法）；`not_found`（SKU 不存在）；
  `invalid_transition`（**已是目标状态**——空操作不报成功，让模型如实转述"无需变更"）
