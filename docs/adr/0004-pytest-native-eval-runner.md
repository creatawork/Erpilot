# ADR-0004：评测 runner 用 pytest 自建，v1 全程序化判分

- 状态：已接受
- 日期：2026-10-05

## 背景

M3 第 4 周（计划 §6）交付评测集起步。方案空间：DeepEval / promptfoo 等现成评测
框架、Langfuse datasets、还是自建。评测要进 CI 回归（计划 §4），预算必须可控，
且三个深方向之一就是"评测体系"——评测本身是作品，不能黑盒交给框架。

## 决策

1. **pytest 就是 runner**：每条 case 一个用例（marker `eval`），报告/预算/
   熔断做成普通 Python 模块（`packages/evals`），不另起 CLI 进程。
2. **case 即代码**：case 用 pydantic 模型定义在 `cases.py`（不另立 YAML），
   字段契约（`EvalCase`）+ 静态校验测试拦截写错的标注。
3. **v1 全程序化判分**：声明式检查项（工具调用、文本包含/排除、步数上限），
   LLM-as-judge 后置到 judge 校准（人工一致率 ≥85%）之后。
4. **预算熔断**：`Budget` 按累计成本（trace 的 run_end 计量）跳过余下 case；
   上限来自 `ERPILOT_EVAL_BUDGET`（CI 设 0.05 元，本地默认 1 元）。
5. **占位符解耦**：case 里不写死单号/SKU，运行时从确定性种子库解析
   （`context.py`）——种子改了 case 不用跟着改。

## 理由

- pytest 自建 vs 评测框架：case 定义、判分口径、报告格式都是要写进面试叙事的
  "原理层"；框架把这三样黑盒化，出了判分歧义无法自证。DeepEval 等 LLM-as-judge
  框架在 v1 也用不上（judge 后置）。
- pytest vs 自写 CLI：CI 接入零成本（marker 排除/选择天然支持）、失败定位/参数化
  /fixture 复用白拿；代价只是"用例函数"这层薄壳。
- 程序化判分先行：成功率曲线（M9 验收线 60%→90%）的分母必须稳定、判定必须可
  复核；LLM judge 判定抖动，先程序化后校准 judge 才能曲线可信。
- trace 依赖：runner 复用 `JsonlTraceRecorder` 落盘每条 case 的完整 trace——
  失败 case 用 `erpilot replay` 直接回放归因，评测失败 ≠ 不可解释。

## 后果

- case 写法有学习成本（字段语义见 docs/eval-annotation-guide.md），但有静态
  校验兜底；
- 文本类"回答得体"的检查项收不进 v1 评测集，等 judge 校准后补（标注标准 §5）；
- CI 评测依赖 secrets（ZHIPU_API_KEY 等），未配置时自动跳过——开源仓库 CI
  不烧钱也不红。
