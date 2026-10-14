"""原币种计费估价（M06 支线）。

价目按「模型 + 当前端点主机」解析；只有已确认价目的组合才允许计费路径，
其余一律拒绝——宁可不比，不能编价目。估算结果保留原币种（USD 不换算成
CNY，也不与人民币直接比较数值）；缺失 usage 不当作 0 费用。

旧 CNY 成本口径在 agent_core.prices，本模块不迁移也不改动它。
"""

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from agent_core.llm import StreamEnd, Usage
from pydantic import BaseModel

_TOKENS_PER_M = Decimal(1_000_000)
_AMOUNT_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class EndpointRate:
    """一个已确认价目的模型费率（每百万 token，原币种）。"""

    model: str
    endpoint_host: str
    currency: str
    input_per_m: Decimal
    output_per_m: Decimal
    cached_input_per_m: Decimal | None = None
    price_source: str = "unconfirmed"

    def __post_init__(self) -> None:
        if not self.model or not self.endpoint_host or not self.currency:
            raise ValueError("价目必须提供模型、端点主机与币种")
        for field in ("input_per_m", "output_per_m"):
            self._check_finite_non_negative(field, getattr(self, field))
        if self.cached_input_per_m is not None:
            self._check_finite_non_negative("cached_input_per_m", self.cached_input_per_m)

    @staticmethod
    def _check_finite_non_negative(field: str, value: Decimal) -> None:
        if not value.is_finite() or value < 0:
            raise ValueError(f"非法价目 {field}={str(value).lower()}：必须是有限的非负十进制数")


class BillingEstimate(BaseModel):
    """一次用量按原币种价目的估算；金额以十进制字符串保存，未知为 None。"""

    currency: str
    estimated_amount: str | None
    pricing_source: str
    cache_assumption: str
    complete: bool


_RATE_TABLE: dict[tuple[str, str], EndpointRate] = {
    # DeepSeek 中转端点价目：用户 2026-10-12 确认，USD / 百万 token，
    # 缓存写入价 0（无独立费率项，拆分公式不需要它）。
    (
        "deepseek-v4.1-flash",
        "flashcoding.ai",
    ): EndpointRate(
        model="deepseek-v4.1-flash",
        endpoint_host="flashcoding.ai",
        currency="USD",
        input_per_m=Decimal("0.045"),
        output_per_m=Decimal("0.18"),
        cached_input_per_m=Decimal("0.024"),
        price_source="user-confirmed-2026-10-12",
    ),
    # GLM 人民币估价沿用 agent_core.prices 的维护口径（非实测账单）。
    (
        "glm-5.3-flash",
        "flashcoding.ai",
    ): EndpointRate(
        model="glm-5.3-flash",
        endpoint_host="flashcoding.ai",
        currency="CNY",
        input_per_m=Decimal("0.096"),
        output_per_m=Decimal("0.34"),
        price_source="agent_core.prices",
    ),
}


def endpoint_rate(model: str, base_url: str) -> EndpointRate:
    """按模型与当前端点主机解析价目；没有已确认价目的组合直接拒绝。"""
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        host = ""
    host = host.strip("[]")
    rate = _RATE_TABLE.get((model.lower(), host))
    if rate is None:
        raise ValueError(
            f"价目：没有 {model} 在端点主机 {host or '未知'} 的已确认价目；拒绝计费路径"
        )
    return rate


def quote_usage(
    rate: EndpointRate,
    usage: Usage | None,
    cached_prompt_tokens: int | None = None,
) -> BillingEstimate:
    """把一次用量按原币种价目拆分计价；usage 缺失时如实报告未知。"""
    if usage is None:
        return BillingEstimate(
            currency=rate.currency,
            estimated_amount=None,
            pricing_source=rate.price_source,
            cache_assumption="unknown_usage",
            complete=False,
        )
    if cached_prompt_tokens is None:
        cached = 0
        assumption = "uncached_upper_bound"
    else:
        if isinstance(cached_prompt_tokens, bool) or not isinstance(cached_prompt_tokens, int):
            raise TypeError(
                f"cached_prompt_tokens 需为整数，"
                f"收到 {type(cached_prompt_tokens).__name__}"
            )
        if cached_prompt_tokens < 0:
            raise ValueError(f"非法缓存量 {cached_prompt_tokens}：不能为负")
        if cached_prompt_tokens > usage.prompt_tokens:
            raise ValueError(
                f"非法缓存量 {cached_prompt_tokens}：超过本次输入量 {usage.prompt_tokens}"
            )
        cached = cached_prompt_tokens
        assumption = "observed" if cached > 0 else "uncached"
    billed_input = Decimal(usage.prompt_tokens - cached) * rate.input_per_m
    cached_rate = rate.cached_input_per_m
    cached_amount = (
        Decimal(cached) * cached_rate if cached and cached_rate is not None else Decimal(0)
    )
    billed_output = Decimal(usage.completion_tokens) * rate.output_per_m
    amount = (billed_input + cached_amount + billed_output) / _TOKENS_PER_M
    # 有用量即可覆盖全部 token：缓存未知只影响上界口径，费用不是缺失。
    return BillingEstimate(
        currency=rate.currency,
        estimated_amount=str(amount.quantize(_AMOUNT_QUANTUM)),
        pricing_source=rate.price_source,
        cache_assumption=assumption,
        complete=True,
    )


def _merge_estimates(estimates: list[BillingEstimate]) -> BillingEstimate:
    """把同一 case 的逐请求估价合并为增量：任何未知请求都让总额未知。"""
    head = estimates[0]
    if any(e.estimated_amount is None for e in estimates):
        return BillingEstimate(
            currency=head.currency,
            estimated_amount=None,
            pricing_source=head.pricing_source,
            cache_assumption="unknown_usage",
            complete=False,
        )
    assumptions = {e.cache_assumption for e in estimates}
    total = sum((Decimal(e.estimated_amount) for e in estimates), Decimal(0))
    return BillingEstimate(
        currency=head.currency,
        estimated_amount=str(total.quantize(_AMOUNT_QUANTUM)),
        pricing_source=head.pricing_source,
        cache_assumption=next(iter(assumptions)) if len(assumptions) == 1 else "mixed",
        complete=all(e.complete for e in estimates),
    )


class BudgetExhausted(RuntimeError):
    """计费预算已尽（金额、未知计量或请求次数任一触界）；下一次请求不得发出。"""


class CurrencyBudget:
    """原币种预算熔断：金额上限 + 请求次数上限 + 未知计量即停。

    与旧 CNY `Budget` 并行存在（M06-03 之前旧路径保持原行为）。
    """

    def __init__(self, limit: Decimal, currency: str, max_requests: int) -> None:
        if not limit.is_finite() or limit < 0:
            raise ValueError(f"非法预算上限 {limit}：必须是有限的非负十进制数")
        if not currency:
            raise ValueError("预算币种不能为空")
        if max_requests <= 0:
            raise ValueError(f"非法请求熔断上限 {max_requests}：必须为正整数")
        self.limit = limit
        self.currency = currency
        self.max_requests = max_requests
        self._spent = Decimal(0)
        self.requests = 0
        self.unknown_usage = False

    @property
    def spent(self) -> Decimal:
        return self._spent

    @property
    def exhausted(self) -> bool:
        return (
            self.unknown_usage
            or self.requests >= self.max_requests
            or self._spent >= self.limit
        )

    def reason(self) -> str:
        if self.unknown_usage:
            return "存在计量未知的请求（缺失 usage 已按可能计费处理），已停止后续请求"
        if self.requests >= self.max_requests:
            return f"请求次数达到熔断上限（{self.requests} ≥ {self.max_requests}）"
        return f"已花 {self.spent.quantize(_AMOUNT_QUANTUM)} {self.currency} ≥ 上限 {self.limit} {self.currency}"

    def before_request(self) -> None:
        """每次真实请求前调用：预算已尽抛 BudgetExhausted，占用一个请求名额。"""
        if self.exhausted:
            raise BudgetExhausted(f"计费预算已尽：{self.reason()}")
        self.requests += 1

    def record(self, estimate: BillingEstimate | None) -> None:
        """记录一次请求的估价；缺失金额按未知计量处理，不算零费用。"""
        if estimate is None or estimate.estimated_amount is None:
            self.unknown_usage = True  # 不知道费用 ≠ 没有费用
            return
        if estimate.currency != self.currency:
            raise ValueError(
                f"预算币种 {self.currency} 与费用币种 {estimate.currency} 不同，拒绝混合计量"
            )
        self._spent += Decimal(estimate.estimated_amount)


class BudgetedClient:
    """对 LLMClient 的计费包装：请求前查预算，流结束按原币种计量。

    暴露与原客户端相同的 `config` 与 `stream_chat`。run_case 每条 case 开始
    时调用 `begin_case()`，结束时读取 `case_billing()` 取本条增量；
    baseline 不得在 case 之外重复累加。
    """

    def __init__(self, client: object, rate: EndpointRate, budget: CurrencyBudget) -> None:
        if rate.currency != budget.currency:
            raise ValueError(
                f"预算币种 {budget.currency} 与价目币种 {rate.currency} 不同，拒绝混算"
            )
        self._client = client
        self._rate = rate
        self._budget = budget
        self._case_estimates: list[BillingEstimate] = []

    @property
    def config(self) -> object:
        return self._client.config  # type: ignore[attr-defined]

    @property
    def budget(self) -> CurrencyBudget:
        return self._budget

    def begin_case(self) -> None:
        """run_case 开头复位增量窗口。"""
        self._case_estimates = []

    def case_billing(self) -> BillingEstimate | None:
        """本 case 的计费增量（含该 case 全部请求）；无请求时返回 None。"""
        if not self._case_estimates:
            return None
        return _merge_estimates(self._case_estimates)

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        self._budget.before_request()  # 预算已尽：请求根本不发出去
        usage: Usage | None = None
        settled = False
        try:
            async for event in self._client.stream_chat(messages, tools=tools):
                match event:
                    case StreamEnd() as end:
                        usage = end.usage
                yield event
            estimate = quote_usage(self._rate, usage)
            self._budget.record(estimate)
            self._case_estimates.append(estimate)
            settled = True
        finally:
            if not settled:
                # 流中断：请求可能已计费但用量未知——按未知处理并停止后续请求。
                self._budget.record(None)
                self._case_estimates.append(quote_usage(self._rate, None))
