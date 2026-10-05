"""evals：评测体系（三个深方向之一，M3 起步、贯穿全程）。

铁律：先写人工标注标准，再写 case（docs/eval-annotation-guide.md）；
judge 与人工一致率 <85% 就停下校准。

- 评测集 50+ 条，四类：single 单工具 / multi 多步 / edge 边界 / adversarial 对抗
- 指标：任务成功率、工具调用正确率、错误自愈率、成本/任务、P95 延迟
- 每次改 prompt / 换模型 / 调工具都跑回归；CI 里只用便宜模型、设预算熔断
- 产出物：一条上涨的成功率曲线 + 每次跃升对应的改动记录

模块分工：

- model.py   ：EvalCase / CaseResult（pydantic，标注的字段契约）
- context.py ：占位符 → 种子库真实数据（标注与数据解耦）
- checks.py  ：声明式检查项 → 通过/失败（v1 全程序化，LLM-as-judge 后置）
- cases.py   ：评测集本体（人工标注，进集规则见标注标准 §6）
- runner.py  ：跑一条 case：真 LLM + MCP 工具 + trace 落盘 + 预算记账
- report.py  ：markdown 回归报告（reports/evals/）
"""

from evals.model import CaseCategory, CaseResult, EvalCase
from evals.runner import Budget, run_case

__all__ = ["Budget", "CaseCategory", "CaseResult", "EvalCase", "run_case"]
