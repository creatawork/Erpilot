# F05：失败、判因与定点复测

## 观察

2026-10-08 的 v2.2 全量回归执行了固定的 35 条 case，32 条通过（32/35，约 91%）。原始报告保留在[全量运行报告](../reports/evals/20261008-104243-fdd8ba.md)，对应的逐 case trace 仍由报告中的 `case_traces` 索引。此前一次全量运行中的接口超时与断连也有单独记录；这些暂态失败和后续运行没有合并计分。

## 判因

逐条复核 v2.2 的 adv-02、adv-05、adv-06 失败 trace 后，发现三项失败来自判分规则没有覆盖等价措辞或有效工具路径，而不是这三条 trace 显示了错误业务行为。具体判因与规则变化记录在[评测标注指南 F05](eval-annotation-guide.md#53-f05-真实基线措辞复核-scorer-v23v24-2026-10-08)及[错误恢复日志](error-recovery-log.md)：adv-02 的“没搜到”属于有效的空结果说明；adv-05 的 `compare_quotes` 是可接受的报价工具路径；adv-06 的同义拒绝措辞应满足拒绝判据，同时手机号泄露正则仍保留。

## 修正规则后的证据

v2.3 将 adv-02 与 adv-05 作为定点用例重测，两条通过；同一报告中的 adv-06 仍失败，因此该报告的结果是 2/3，而非三条全过：[v2.3 定点报告](../reports/evals/20261008-110631-3b5f1f.md)。之后 v2.4 对 adv-06 单独重测并通过：[v2.4 定点报告](../reports/evals/20261008-110815-1a9599.md)。

这些报告证明的是各自版本、各自 case 集和各自运行中的结果。它们不能与 v2.2 的 32/35 全量分数相加或拼接成一次“35/35”全量结果；现有材料没有提供修正规则后的全量复跑证据。

## 可复现趋势

[`trends-v1.json`](../reports/evals/trends-v1.json)、[Markdown 表格](../reports/evals/trends-v1.md)和[SVG 图](../reports/evals/trends-v1.svg)由仓库现存逐次 JSON 报告聚合生成。不同 scorer、suite、prompt 与 case 指纹保持分 cohort；没有足够元数据的旧报告保留为 unknown 观察，不作为可比曲线点。
