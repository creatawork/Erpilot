"""模型价目表与成本估算（人民币 / 每百万 tokens）。

价格以在用端点的实际计费为准，此处只维护在用的模型：
GLM-5.3-Flash 当前按输入 0.096 / 输出 0.34 维护（2026-09-29）。
未收录的模型返回 None——宁可不算，不要算错。
"""

from dataclasses import dataclass

from agent_core.llm import Usage


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_m: float  # 输入：元 / 百万 tokens
    output_per_m: float  # 输出：元 / 百万 tokens


PRICES: dict[str, ModelPrice] = {
    "glm-5.3-flash": ModelPrice(input_per_m=0.096, output_per_m=0.34),
}


def cost_of(model: str, usage: Usage | None) -> float | None:
    """估算单次调用成本（元）；模型未收录或无 usage 时返回 None。"""
    price = PRICES.get(model.lower())
    if price is None or usage is None:
        return None
    return usage.prompt_tokens / 1_000_000 * price.input_per_m + (
        usage.completion_tokens / 1_000_000 * price.output_per_m
    )
