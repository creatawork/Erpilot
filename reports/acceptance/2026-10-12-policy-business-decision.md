# B5 政策与订单事实联合判断：工程验收

日期：2026-10-12。工程验收通过，模型评测未运行；最终独立审阅正在进行。

已实现 `assess_order_cancellation(order_id)`，复用领域 `CANCELLABLE_STATUSES`。它只查询订单和当前有效政策，不建立预览、审批或执行令牌。网页显示当前状态、原金额、查询时间和可展开的版本引用；资格问题仅给出“可发起取消申请 / 当前不可取消 / 政策待核对”。明确取消后仍走原保存预览、人工审批与事务复核。

## 运行证据

| 检查 | 结果 |
|---|---|
| Python 全量回归 | 636 passed / 21 skipped / 47 deselected，62.52 秒 |
| Ruff | `ruff check .` 通过 |
| 前端协议 / 组件测试 | 77 passed |
| 前端生产构建 | 通过；JS gzip 413.70 kB，保留既有大包提示，不放宽阈值 |
| Chromium 浏览器 | 12 passed，16.6 秒；新卡覆盖 320 / 768 / 1024 / 1440 px、键盘展开、历史恢复 |
| 差异空白检查 | 最终检查待记录 |
| 模型 / 嵌入付费请求 | 0；GLM / DeepSeek 真实工具选择和语言准确率未测量 |

Python 使用项目 `.venv` 的 pytest，配置仍排除 `eval`，47 项真实评测未执行；21 项跳过都需要显式配置隔离 PostgreSQL（3 项 checkpoint、18 项硬进程恢复）。未降低任何测试断言或改变跳过规则。Windows 使用系统 Temp 下全新且验证过的 basetemp，避免 ACL 限制及仓库相对路径改变 trace 测试口径。

日志保留在 `.superpowers/sdd/2026-10-12-policy-business-decision/`，最终全量为 `test-bbc75a99c6.log`。浏览器使用本机 `chromium-1243/chrome-win64/chrome.exe`，通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 显式指定；版本待记录。UI 截图位于 `.playwright-mcp/b5-card-320.png` 与 `b5-card-1440.png`，已人工检查文本换行、键盘焦点及引用转义。CI 配置此前已接入浏览器，远端 CI 未运行。

## P01–P14 证据映射

下表只说明确定性业务与协议证据。脚本客户端按预设选择工具，不能证明真实模型会正确理解自然语言或抵抗提示注入。

| ID | 已验证路径 |
|---|---|
| P01 | `test_status_and_evidence_matrix` 的两允许状态；`test_assessment_has_no_database_side_effects` 四种 MCP 工具面，真实金额 19.50 |
| P02 | 同一六状态矩阵的四禁止状态；注入片段也不能覆盖已发货领域许可 |
| P03 | `test_bad_order_returns_before_resolver` 空白 / 缺失订单提前返回，不调用 resolver |
| P04 | `test_ambiguous_script_clarifies_without_writing` 两候选订单、脚本返回澄清、全库不变；CLI 有工具时才追加同一指引 |
| P05 | `test_embedding_failure_is_sanitized`、`test_policy_unavailable_preserves_order_facts`；默认 resolver 拒绝缺失 / 陈旧索引 |
| P06 | `test_search_policy_refuses_out_of_corpus` 与 calibrated threshold 回归；空 / 无关规则证据不能 eligible |
| P07 | `test_policy_metadata.py` 左闭右开区间；`test_policy_evidence.py` 过期高分先排除、未来与截止边界；UTC 引用偏移归一测试 |
| P08 | 元数据 / 哈希 / 来源校验及 refresh 正文、标题、来源、顺序、数量变化拒绝；旧索引不产生引用 |
| P09 | `test_conflict_precedes_top_k` 同版本多小节去重、重叠版本与哈希冲突；纯函数校验结构化规则与领域值集合一致 |
| P10 | `test_instruction_in_policy_never_changes_domain_permission_or_writes` 两状态下全库不变；恶意文本按 React 文本节点展示；真实模型抵抗能力待测 |
| P11 | `test_query_restore_then_explicit_cancel_requires_approval` 查询后拒绝明确取消；HTTP snapshot 和历史 display 一致，订单待付款、库存 98 |
| P12 | `test_prior_query_does_not_authorize_changed_order` 查询后变为已发货，原预览拒绝；原 operations/API 条件变化执行回归 |
| P13 | 原 `test_operations.py` / `test_operations_api.py` 重复提交、提交后响应丢失、重建宿主、过期和跨会话回归；历史评估不重新检索 |
| P14 | 严格 display 数字 / 枚举 / 时间 / 许可一致性测试，未知形状回退原文；组件测试与 5 个新浏览器用例 |

全表副作用证据：临时 ERP 中先建立商品、库存和订单，调用前后比较 SQLite `iterdump()` 全文完全相等，包含所有草稿、操作预览、mutation token 表。未只抽样库存。应用 trace/checkpoint 的记录属于正常查询运行记录，不是 ERP 变更。

## source_provenance

语料来源为仓库随版本维护的三份 Markdown 和 `catalog.json`；版本 `2026-10-12.1`，生效时间 `2026-10-11T16:00:00Z`，规则 `order.cancel` 绑定当前两条可取消状态及对应小节。哈希以 UTF-8 正文 LF 归一后计算；没有由模型生成引用。

| 来源 | 归一正文 SHA-256 |
|---|---|
| `order-policy.md` | `ec768d3c162e8ffa8a36ca24126afdf1024ab894bee3e847c379b9ed1779c290` |
| `pricing-policy.md` | `dff3357f163cc8b8e8cb30e36fee0d8401eb0249e1eab36aeef091b7d955910b` |
| `return-policy.md` | `50a87a110e77aed17e6d6473382aaa961d9531a60f9dc1be84e4110401a4617c` |

- catalog 文件字节 SHA-256：`f5be1de67facd3f6e4eac5f1f36c4857eac8be4bab2b0121a1c2b0a868628c75`。
- 更新后 index 文件字节 SHA-256：`b5b5c47d9e6d29a116afd971087fbe82c5df477e7ae66c6ea225a8863813d776`。
- 片段与向量均 18 条。刷新前后 `(source,title,text)` 有序列表和每条向量完全相等，原向量零重嵌入；刷新失败时原输出保留。维度不一致或非有限向量明确不可用。

## 实施裁决与边界

1. 沿用 `codex/business-flow-pricing` 当前工作目录，因为已接受的界面基线尚未提交；保留原改动快照。代价是集成前需要一起审阅前端基线，不能把无关改版混入 B5 提交。
2. Windows 的 Bash 记账指令改用同一计划目录内的 Python helper；保留实际命令、失败日志和任务简报，避免恢复时重复实施。
3. 使用全新系统 Temp 路径，不复用或删除别的会话目录；初次仓库内 basetemp 引起已有 trace 相对路径测试失败，外部路径复测通过。代价是本机临时目录 ACL 仍需验证。
4. 新增检索向量维度 / 有限数检查，因为失败测试证实 cosine 会截断错配维度并错误命中；代价是旧畸形向量现在明确拒绝。
5. 前端 B5 改动保持未提交，与用户的 Ant Design 工作台基线共同审阅；后端 B5 已选择性提交。不为凑独立提交而提交无关文件。
6. 额外在现有 App.css 加三条局部样式，并在 AssistantMessage 让有效评估默认展开；首轮浏览器 5 项失败证明默认工具折叠藏住结论。用户仍可折叠，政策引用单独展开。
7. 引用时间统一 UTC Z；带其他合法时区的测试先失败再修复。时间归一不改变生效时点。
8. P04/P10 的模型选择只由 HTTP 脚本模拟；M06 计费及真实模型对照独立，保持待办，未修改 `.env`、默认模型或密钥。

本次新增卡片和协议无需新依赖或数据库表。不支持任意政策正文语义自动审计、政策后台、退款执行、多租户、部署。真实模型预算、原币种计费、GLM/DeepSeek 对照和 B4 多轮指代准确率另行执行。

## 最终审阅

按 `executing-plans` 的一次独立只读审阅进行，结果待记录。前端基线整体提交、远端 CI 和真实模型评测仍待完成。
