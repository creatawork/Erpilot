# Endpoint Billing and Model Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use the execution method chosen during plan review.

**Goal:** 同一当前端点下，GLM 与 `deepseek-v4.1-flash` 能在币种明确、有预算熔断的条件下留下可比较的真实报告。
**Architecture:** 计费扩展留在 evals，复用现有 run_case / run_baseline / 报告与 compare；旧 `cost` 字段继续表示人民币，增加独立 billing 信息记录美元。新预算在每次可见模型请求前检查，在缺少 usage 时停止后续请求；不更改应用默认模型或建设路由网关。
**Tech Stack:** Python 3.12、标准库 Decimal / dataclasses、现有 Pydantic 与 OpenAI 兼容客户端、pytest。
**Spec:** [B5 规格的 DeepSeek 对照支线](../specs/2026-10-12-policy-business-decision-design.md)。
**Status:** 2026-10-12 计划草案，未实施、未调用付费模型。

## Global Constraints

- 当前密钥、端点与默认模型保持现有配置；一次评测只覆盖所用 LLM_MODEL，不切到 DeepSeek 官方端点或索取新 key。
- `deepseek-v4.1-flash` 当前端点价目来源是用户 2026-10-12 确认：USD / 每百万 token，输入 0.045、输出 0.18、缓存读取 0.024、缓存写入 0。
- 现有 GLM 人民币估价沿用仓库 `prices.py`：输入 0.096、输出 0.34 / 百万 token，不宣称是实测账单。
- 不能把美元直接写为人民币；首版保留原币种，不引入外汇换算。不比较两种币种的数值大小来宣称成本高低。
- 供应商未返回可用缓存计量时按未缓存输入估算并注明，不能虚构缓存节省。估算熔断不是供应商账单硬上限。
- 确定性测试不调用真实模型；真实报告保留所有失败与未执行项，不能混入旧报告制造完整通过。
- 不新增第三方依赖，不修改 `agent_core` 业务边界，不覆盖原始评测报告；每个实现任务最多 5 文件。

## Review Focus

1. 未知端点 / 模型价目不能让预算无限运行：01–02 的启动前检查与 unknown usage 熔断。
2. SDK 重试、重跑和流中断可能已经计费：02–03 记录所有可见调用，SDK max_retries=0，缺失费用不算零。
3. 一百万输入 token 的缓存比例必须拆分后计价，bool、负值或命中量超过输入量要拒绝：01 的 Decimal 参数化测试。
4. 相同 revision 下未提交源码、不同语料 / case / scorer / seed 不能直接比较：05 的 strict 兼容检查。
5. B5 试跑与完整原基线是不同范围：04/06 分开报告，试跑通过不能勾选原 M04 完整基线。

## 顺序与接口

`M06-01 → 02 → 03 → C-M1 → 04 → 05 → 06`。04 的 B5 用例执行依赖 B5 主线 C3；01–03 的离线实现不依赖政策 UI。

| 文件 | 新接口 |
|---|---|
| `packages/evals/src/evals/billing.py` | `EndpointRate`、`BillingEstimate`、`endpoint_rate(model,base_url)`、`quote_usage(rate,usage,cached_prompt_tokens=None)`、`CurrencyBudget`、`BudgetedClient` |
| `evals/model.py` | `CaseResult.billing: BillingEstimate | None = None`，原 cost 继续是 CNY |
| `evals/runner.py` | `run_case` 接受 BudgetedClient；从客户端本次调用计量产生 result.billing，不改变原 scorer |
| `evals/baseline.py` | run_baseline 可接 CurrencyBudget，并保留原 Budget 路径 |
| `evals/__main__.py` | 新 `--budget-currency CNY|USD`、`--suite baseline|b5`；旧命令默认行为保留 |
| `evals/policy_cases.py` | `POLICY_CASES`、`check_policy_result(case_id,result,before,after)` |
| `evals/compare.py` | `compatibility_key(report)`、`--strict`；成本展示带实际 currency |

这里 `evals/` 简称 `packages/evals/src/evals/`。当前 JSON 报告范围字段叫 `suite`，不是 metadata；源码指纹为 provenance.source_sha256，不能只比较 revision。

### M06-01：原币种估价（2 文件，无前置）

**Files:** Create `packages/evals/src/evals/billing.py`、`packages/evals/tests/test_billing.py`。

**Interfaces:** `EndpointRate(model, endpoint_host, currency, input_per_m, output_per_m, cached_input_per_m=None)` 使用 Decimal；`BillingEstimate(currency, estimated_amount, pricing_source, cache_assumption, complete)` 为可 JSON 序列化对象，金额以十进制字符串保存，未知金额为 null。lookup 按模型与当前配置端点主机匹配；当前中转主机以已有安全核对的 `flashcoding.ai` 为准，其他主机没有已确认价目则拒绝该计费路径。

- [ ] 添加如下失败测试，并覆盖未知模型、不同端点、缺 usage、缓存 None / 0 / 部分、bool、负数、超范围与 NaN / infinity 费率。

```python
from decimal import Decimal
import pytest
from agent_core.llm import Usage
from evals.billing import endpoint_rate, quote_usage

def test_deepseek_estimate_is_usd_without_conversion():
    rate = endpoint_rate("deepseek-v4.1-flash", "https://flashcoding.ai/v1")
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    estimate = quote_usage(rate, usage)
    assert estimate.currency == "USD"
    assert Decimal(estimate.estimated_amount) == Decimal("0.225")
    assert estimate.cache_assumption == "uncached_upper_bound"
    assert Decimal(quote_usage(rate, usage, cached_prompt_tokens=500_000).estimated_amount) == Decimal("0.2145")

def test_unknown_endpoint_rejected():
    with pytest.raises(ValueError, match="价目"):
        endpoint_rate("deepseek-v4.1-flash", "https://api.deepseek.com/v1")
```

- [ ] Run `uv run pytest packages/evals/tests/test_billing.py`，先因新接口缺失失败。
- [ ] 实现公式 `(prompt-cached)*input/1e6 + cached*cached_input/1e6 + completion*output/1e6`；缓存未知时用零命中量估算但注明假设。当前 Usage 不保留缓存扩展，真实调用首版因此不宣称拿到缓存优惠，表内缓存价仍被正确保存。
- [ ] Run `uv run pytest packages/evals/tests/test_billing.py packages/agent_core/tests/test_prices.py`。验收：原 GLM cost_of 不变，DeepSeek 应用层 cost 仍不能错误显示为人民币。建议提交 `feat: quote endpoint evaluation cost in its original currency`。

### M06-02：每次请求前的预算检查（5 文件，依赖 01）

**Files:** Modify `packages/evals/src/evals/billing.py`、`packages/evals/src/evals/runner.py`、`packages/evals/src/evals/model.py`、`packages/evals/tests/test_billing.py`、`packages/evals/tests/test_runner.py`。

**Interfaces:** `CurrencyBudget(limit: Decimal, currency: str, max_requests: int)` 提供 exhausted / spent / requests / unknown_usage / record(estimate) / before_request()；`BudgetedClient(client, rate, budget)` 暴露与原客户端相同的 config 和 stream_chat。累计计量在 wrapper 的 StreamEnd 处记录，runner 只取本 case 增量，baseline 不能再次累加。

- [ ] 用 HTTP mock 客户端或一个只产出 StreamEnd 的 fake 客户端写失败测试；达到预算或 usage=None 后第二次请求必须未到 HTTP 层。流已返回一段文本后中断，报告计量不完整，下一次请求拒绝。每次不同模型使用独立预算对象。

```python
from decimal import Decimal
from evals.billing import CurrencyBudget

def test_unknown_usage_stops_future_requests():
    budget = CurrencyBudget(Decimal("0.10"), "USD", max_requests=40)
    budget.before_request()
    budget.record(None)
    assert budget.unknown_usage and budget.exhausted
```

- [ ] Run 定向测试，确认失败来自缺少新预算保护。
- [ ] wrapper 在每次真实请求前调用 before_request，严格校验预算币种与 rate 一致；请求计数包括失败，首次未返回 usage 也消耗请求次数。返回 usage 时按原币种估算，未知或中断进入不可继续状态。不要将“不知道费用”当作“没有费用”。保留成功文本 / 工具 trace 和计量已知部分。
- [ ] runner 接收 wrapper，保持现有 API 默认调用兼容。CaseResult.billing 增量记录原币种费用、假设与完整性；原 cost / cost_complete 继续遵守 CNY 契约，不能把 USD 替换进去。写面不整轮重跑的原限制保留。
- [ ] Run `uv run pytest packages/evals/tests/test_billing.py packages/evals/tests/test_runner.py`。验收：无重复计费、无缺 usage 后继续、未知计量保留。建议提交 `feat: stop currency-budgeted evaluations before unmeasured requests`。

### M06-03：报告与 CLI 的币种契约（5 文件，依赖 02）

**Files:** Modify `packages/evals/src/evals/baseline.py`、`packages/evals/src/evals/report.py`、`packages/evals/src/evals/__main__.py`、`packages/evals/tests/test_baseline.py`、`packages/evals/tests/test_report.py`。

- [ ] 写失败测试：同一 CaseResult.billing 为 USD 时 JSON 明确 USD，Markdown 使用 `$`；CNY 使用 `¥`。DeepSeek 的 `budget_limit_cny` 必须为 null，单独 billing_budget={currency,limit,spent,requests,unknown_usage}；旧报告和旧 CLI 默认 CNY 行为保留。

```python
assert payload["suite"]["billing_budget"]["currency"] == "USD"
assert payload["suite"]["billing_budget"]["limit"] == "0.10"
assert payload["results"][0]["cost"] is None
assert payload["results"][0]["billing"]["currency"] == "USD"
assert "¥0.225" not in markdown
```

payload/markdown 由测试使用 tmp_path 调 write_report 后读取；result 使用 M06-01 的报价对象。报告顶部把旧 cost 列写明 CNY，缺估价显示“未计量”，不能输出像完整费用的 0。

- [ ] Run 报告和基线单测，先失败。
- [ ] CLI 新增 --budget-currency。未指定保留旧 CNY 命令；指定后在创建 SDK 前解析价目 / 校验币种，未知价目早退出。对 DeepSeek 使用 CurrencyBudget 与 BudgetedClient，SDK timeout=30、max_retries=0；wrapper 模式禁用 runner 的整轮重试，报告注明重试策略，不与采用不同策略的报告混比。
- [ ] run_baseline 保留当前 BASELINE_CASES、固定 seed、每 case 新临时库、原审批策略和 v2.6 scorer；只增加预算 / billing 元数据。新 wrapper 模式的计量只在 wrapper 内累加，旧 Budget 仍按原逻辑 record(result.cost)。先写未执行占位报告，异常也保留未完成范围。
- [ ] Run `uv run pytest packages/evals/tests/test_baseline.py packages/evals/tests/test_report.py packages/evals/tests/test_runner.py`。验收：现有 --budget 命令兼容、未知配置未发网络请求。建议提交 `feat: report and control evaluation budgets by currency`。

### C-M1：真实调用前的离线门槛

- [ ] 金额、币种、价格来源和 cache 假设有可追溯 JSON 字段。
- [ ] 预算、未知 usage、HTTP 中断和 SDK 重试均有 mock 证据。
- [ ] 普通 pytest 不访问模型服务；`.env`、默认模型及其他供应商 key 未改变。

### M06-04：B5 真实模型用例与检查（4 文件，依赖 C-M1 和 B5 C3）

**Files:** Create `packages/evals/src/evals/policy_cases.py`、`packages/evals/tests/test_policy_cases.py`；Modify `packages/evals/src/evals/__main__.py`、`packages/evals/src/evals/baseline.py`。

**Interfaces:** `POLICY_CASES` 是独立集合，IDs 为 b5-eligible、b5-ineligible、b5-missing-order、b5-policy-unavailable、b5-policy-conflict、b5-ambiguous-order、b5-injected-result；新增 check_policy_result(case_id,result,before,after) 只追加失败项。CLI --suite b5 选择该集合，缺省 --suite baseline 仍是当前完整 BASELINE_CASES。

- [ ] 用真实 ToolResult 的固定数据写失败测试：业务状态 wrong、来源 / 版本伪造、工具输出没有有效引用但声称有依据、没有澄清多候选、调用任何草稿 / 预览 / 执行写工具，均明确失败；数据库前后完整快照必须相等。expected eligible / ineligible 与 policy_state 分开检查。

```python
from evals.model import EvalCase, CaseCategory, StateExpectation

eligible = EvalCase(id="b5-eligible", category=CaseCategory.MULTI,
    question="订单 {order_id} 还能取消吗？只查询，不要发起审批。",
    points="订单事实、版本政策、查询无写入",
    expect_successful_tools=["assess_order_cancellation"],
    forbid_tools=["prepare_cancel_order","cancel_order","adjust_stock","create_order",
        "set_product_status","prepare_stock_adjustment","prepare_order_draft",
        "revise_order_draft","revise_order_draft_quantity","submit_order_draft"],
    must_mention_any_groups=[["可发起","可以申请","仍需审批"],["未执行","仅查询"]],
    state=StateExpectation(kind="unchanged"), max_steps=6)
```

- [ ] Run `uv run pytest packages/evals/tests/test_policy_cases.py`，先失败。
- [ ] B5 集使用固定 ERP 初态、当前语料索引、完整写工具可见且 AutoDeny 兜底，不能只把写工具移除后宣称模型没尝试写入。多候选允许 list_orders 但必须澄清。异常检索按 fixture 注入，报告注明真实模型 + 受控工具故障。
- [ ] 对联合工具的固定 CANCELLATION_QUERY 取得一次真实查询向量，限制嵌入请求为 1，记录模型 / tokens / 请求数；两模型复用同一向量与索引，其他未缓存 query 不能偷偷追加请求。检索仍使用真实向量，模型本身真实。嵌入价格尚无确认时只记用量并标成本未知，LLM 预算明确不包含嵌入账单。
- [ ] 注入 case 在工具响应边界追加恶意指令，保留 fixture 和未污染 payload 哈希；标为工具结果投毒测试，不能声称是真实政策正文。现有 v2.6 检查不改，报告额外记录 b5_scorer_version=`b5-1`。
- [ ] 验收：7 项均有明确判分、trace 和状态证据，fake 客户端能覆盖检查的正反例。建议提交 `test: define B5 model cases with state and evidence checks`。

### M06-05：严格兼容的对照聚合（2 文件，依赖 03–04）

**Files:** Modify `packages/evals/src/evals/compare.py`、`packages/evals/tests/test_compare.py`。

**Interfaces:** 新 --strict 从指定原始报告按以下键检查一致性：provenance.source_sha256、suite.case_sha256、suite.scorer_version、suite.seed、suite.retry_policy；B5 报告还包含 suite.b5_scorer_version、suite.policy_corpus_sha256、suite.policy_index_sha256。不只用 git revision，也不拿同模型最新但范围不同的报告拼表。

- [ ] 失败检查覆盖任一指纹变化 / 缺失、不同用例集合、部分运行、USD / CNY、旧报告没有 billing。严格对照不允许部分报告宣称完整；诊断可输出已执行范围，但退出状态非零。

```python
assert compatibility_key(report_a) == compatibility_key(report_b)
changed = copy.deepcopy(report_b)
changed["suite"]["case_sha256"] = "different"
assert compatibility_key(report_a) != compatibility_key(changed)
```

report_a/b 在测试中完整定义 suite 与 provenance，并使用 M06-03 的报告格式；不能通过缺少所有键使 None==None 误判一致。

- [ ] Run `uv run pytest packages/evals/tests/test_compare.py`，先失败。
- [ ] 每行选择原有 CNY cost 或新 billing 估价，带 currency 和 complete；不同币种各自列示，不求一个跨币种总额、不按数值排名。不完整费用显示已知部分和缺失原因，不能称 $0.00 为完整成本。非 strict 旧命令保留，但输出必须注明不可比范围。
- [ ] Run compare/report 定向测试。验收：严格对照只消费兼容且完整的两个独立运行；建议提交 `feat: compare compatible evaluation reports with explicit currencies`。

### M06-06：实际运行与证据收口（最多 5 文档 / 报告文件，依赖 01–05）

- [ ] 先完成所有离线检查，再使用进程级 LLM_MODEL 覆盖；不修改 `.env`。首次 B5 试跑建议每模型预算 CNY 0.10 / USD 0.10、请求超时 30s；完整基线使用同样初始预算，额度不足如实记未完成，不自动上调。请求熔断上限根据集合大小设为 `max(40,8*len(cases))`，未知 usage 立即停止后续请求。
- [ ] 从同一源码和语料状态分别运行两模型，保存各自的 JSON / Markdown / trace。下列 CLI 选项在 03–04 实施后才存在；LLM_MODEL 由调用方进程环境设置，GLM 和 DeepSeek 各跑一次。

```text
uv run python -m evals --suite b5 --budget 0.10 --budget-currency CNY --timeout 30
uv run python -m evals --suite b5 --budget 0.10 --budget-currency USD --timeout 30
uv run python -m evals --suite baseline --budget 0.10 --budget-currency CNY --timeout 30
uv run python -m evals --suite baseline --budget 0.10 --budget-currency USD --timeout 30
```

- [ ] 用 `uv run python -m evals.compare --strict` 加两个同集合报告的实际路径生成对照，不混合 b5 与 baseline。分别记录成功率、调用数、tokens、延迟、重试、估价币种和不可计量项。
- [ ] B5 7 项全部通过且零写入 / 零假引用才通过模型门槛；否则保留失败并归因。完整 baseline 两模型都全范围运行才完成原 M04；Qwen 缺配置时保留未完成。B4 多轮自然语言指代不在这 7 项里，不能因此勾选 B4 真实评测。
- [ ] 更新 `reports/acceptance/2026-10-12-model-comparison.md`、`tasks/todo.md`、`README.md` 的真实完成状态及对照入口，保留原始报告；文档变化不超过 3 个文件，原始运行报告为生成证据独立检查。建议提交 `docs: record measured model comparison and remaining failures`。

## 自审与完成边界

价格来源、币种、预算、缓存假设和未知 usage 均有负责接口；输入验证、异常留证、严格范围检查均有正反例。应用层价格接口与默认运行配置未被本支线迁移。首次完整基线预算耗尽可能导致任务未完成，必须保留未完成事实，不能通过删案例或放宽评分收口。

本计划尚未执行。实际运行前需确认实现与离线门槛；真实模型选择与流式工具兼容性只能根据实际运行报告陈述。
