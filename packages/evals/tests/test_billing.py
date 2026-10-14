"""M06-01/02 原币种估价与预算熔断：USD 不冒充 CNY，未知计量拒绝继续，预算耗尽不发请求。"""

from decimal import Decimal

import pytest
from agent_core.llm import LLMClient, LLMConfig, StreamEnd, TextDelta, Usage

from evals.billing import (
    BillingEstimate,
    BudgetExhausted,
    BudgetedClient,
    CurrencyBudget,
    EndpointRate,
    endpoint_rate,
    quote_usage,
)

_RELAY = "https://flashcoding.ai/v1"


def test_deepseek_estimate_is_usd_without_conversion() -> None:
    rate = endpoint_rate("deepseek-v4.1-flash", _RELAY)
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    estimate = quote_usage(rate, usage)
    assert estimate.currency == "USD"
    assert Decimal(estimate.estimated_amount) == Decimal("0.225")
    assert estimate.cache_assumption == "uncached_upper_bound"
    assert estimate.complete is True
    split = quote_usage(rate, usage, cached_prompt_tokens=500_000)
    assert Decimal(split.estimated_amount) == Decimal("0.2145")
    assert split.cache_assumption == "observed"


def test_unknown_endpoint_rejected() -> None:
    # 没有已确认价目的主机走不了计费路径：宁可不比，不能编价目。
    with pytest.raises(ValueError, match="价目"):
        endpoint_rate("deepseek-v4.1-flash", "https://api.deepseek.com/v1")


def test_unknown_model_rejected() -> None:
    with pytest.raises(ValueError, match="价目"):
        endpoint_rate("qwen-max", _RELAY)


def test_glm_rate_keeps_cny_and_matches_agent_core_prices() -> None:
    from agent_core.prices import PRICES

    rate = endpoint_rate("glm-5.3-flash", _RELAY)
    assert rate.currency == "CNY"
    assert rate.input_per_m == Decimal(str(PRICES["glm-5.3-flash"].input_per_m))
    assert rate.output_per_m == Decimal(str(PRICES["glm-5.3-flash"].output_per_m))


def test_missing_usage_stays_unknown_not_zero() -> None:
    rate = endpoint_rate("deepseek-v4.1-flash", _RELAY)
    estimate = quote_usage(rate, None)
    assert estimate.estimated_amount is None
    assert estimate.complete is False
    assert estimate.cache_assumption == "unknown_usage"


def test_zero_cache_is_exact_not_an_assumption() -> None:
    rate = endpoint_rate("deepseek-v4.1-flash", _RELAY)
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    estimate = quote_usage(rate, usage, cached_prompt_tokens=0)
    assert Decimal(estimate.estimated_amount) == Decimal("0.225")


@pytest.mark.parametrize(
    "cached",
    [-1, 1_500_000],
    ids=["negative", "exceeds_prompt"],
)
def test_invalid_cache_amounts_rejected(cached: int) -> None:
    rate = endpoint_rate("deepseek-v4.1-flash", _RELAY)
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    with pytest.raises(ValueError):
        quote_usage(rate, usage, cached_prompt_tokens=cached)


def test_bool_cache_rejected() -> None:
    rate = endpoint_rate("deepseek-v4.1-flash", _RELAY)
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    with pytest.raises(TypeError):
        quote_usage(rate, usage, cached_prompt_tokens=True)


@pytest.mark.parametrize("field", ["input_per_m", "output_per_m", "cached_input_per_m"])
def test_non_finite_or_negative_rates_rejected(field: str) -> None:
    base = {
        "model": "deepseek-v4.1-flash",
        "currency": "USD",
        "input_per_m": Decimal("0.045"),
        "output_per_m": Decimal("0.18"),
        "cached_input_per_m": Decimal("0.024"),
        "endpoint_host": "flashcoding.ai",
    }
    for label, value in [("nan", Decimal("nan")), ("inf", Decimal("inf")), ("-1", Decimal("-1"))]:
        with pytest.raises(ValueError, match=label):
            EndpointRate(**{**base, field: value})


def test_current_client_model_rate_matches_config() -> None:
    """按当前配置解析价目：模型 / 端点都没配错时才允许后续预算检查。"""
    config = LLMConfig(api_key="test-key", model="deepseek-v4.1-flash", base_url=_RELAY)
    rate = endpoint_rate("deepseek-v4.1-flash", config.base_url)
    assert isinstance(rate.model, str)
    assert rate.currency == "USD"
    client = LLMClient(config, client=None)
    endpoint_rate(client.config.model, client.config.base_url)


# ---- M06-02：CurrencyBudget 与 BudgetedClient ----


def _rate(**overrides) -> EndpointRate:
    base = {
        "model": "test-model",
        "endpoint_host": "test.local",
        "currency": "USD",
        "input_per_m": Decimal("0.045"),
        "output_per_m": Decimal("0.18"),
        "price_source": "test-fixture",
    }
    return EndpointRate(**{**base, **overrides})


class _StubStreamClient:
    """只按剧本产事件的假客户端：Usage → 完整流；异常 → 流中断；None 剧本 → 缺 usage。"""

    def __init__(self) -> None:
        self.script: list[Usage | None | BaseException] = []
        self.requests = 0
        self.config = LLMConfig(api_key="x", model="test-model", base_url="https://test.local/v1")

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        spec = self.script[self.requests] if self.requests < len(self.script) else None
        self.requests += 1
        if isinstance(spec, BaseException):
            yield TextDelta("部分文本")
            raise spec
        usage = spec if isinstance(spec, Usage) else None
        yield TextDelta("完成")
        yield StreamEnd(usage=usage)


def test_unknown_usage_stops_future_requests() -> None:
    budget = CurrencyBudget(Decimal("0.10"), "USD", max_requests=40)
    budget.before_request()
    budget.record(None)
    assert budget.unknown_usage and budget.exhausted


def test_budget_exhausts_at_spent_limit_and_at_request_cap() -> None:
    estimate = BillingEstimate(
        currency="USD", estimated_amount="0.03", pricing_source="t",
        cache_assumption="uncached_upper_bound", complete=True,
    )
    budget = CurrencyBudget(Decimal("0.03"), "USD", max_requests=40)
    budget.before_request()
    budget.record(estimate)
    assert budget.spent == Decimal("0.03")
    with pytest.raises(BudgetExhausted):
        budget.before_request()
    requests_only = CurrencyBudget(Decimal("1"), "USD", max_requests=1)
    requests_only.before_request()
    with pytest.raises(BudgetExhausted, match="请求"):
        requests_only.before_request()


def test_budget_rejects_foreign_currency_estimate() -> None:
    budget = CurrencyBudget(Decimal("1"), "USD", max_requests=40)
    estimate = BillingEstimate(
        currency="CNY", estimated_amount="0.10", pricing_source="t",
        cache_assumption="uncached_upper_bound", complete=True,
    )
    with pytest.raises(ValueError, match="币种"):
        budget.record(estimate)


def test_budget_validates_construction() -> None:
    with pytest.raises(ValueError, match="预算"):
        CurrencyBudget(Decimal("-1"), "USD", max_requests=10)
    with pytest.raises(ValueError, match="预算"):
        CurrencyBudget(Decimal("1"), "", max_requests=10)
    with pytest.raises(ValueError, match="请求"):
        CurrencyBudget(Decimal("1"), "USD", max_requests=0)


def test_budgeted_client_rejects_currency_mismatch() -> None:
    with pytest.raises(ValueError, match="币种"):
        BudgetedClient(_StubStreamClient(), _rate(currency="USD"), CurrencyBudget(Decimal("1"), "CNY", max_requests=10))


async def test_wrapper_quotes_streams_and_blocks_exhausted_next_request():
    client = _StubStreamClient()
    client.script = [Usage(1_000_000, 1_000_000, 2_000_000), Usage(0, 0, 0)]
    budget = CurrencyBudget(Decimal("0.10"), "USD", max_requests=40)
    wrapper = BudgetedClient(client, _rate(), budget)

    events = []
    async for event in wrapper.stream_chat([]):
        events.append(event)
    assert isinstance(events[-1], StreamEnd) and events[-1].usage is not None
    assert budget.requests == 1
    assert Decimal(budget.spent) == Decimal("0.2250")
    with pytest.raises(BudgetExhausted):
        async for _ in wrapper.stream_chat([]):
            pass
    assert client.requests == 1  # 第二次请求未到客户端，也未计数为已发请求
    assert budget.requests == 1


async def test_wrapper_marks_unknown_when_stream_dies_midway():
    client = _StubStreamClient()
    client.script = [RuntimeError("作为流片段后中断")]
    budget = CurrencyBudget(Decimal("1"), "USD", max_requests=40)
    wrapper = BudgetedClient(client, _rate(), budget)
    with pytest.raises(RuntimeError):
        async for _ in wrapper.stream_chat([]):
            pass
    assert budget.unknown_usage and budget.exhausted
    with pytest.raises(BudgetExhausted):
        budget.before_request()  # 下一次请求必须拒绝


async def test_wrapper_case_window_reports_increment_and_resets():
    client = _StubStreamClient()
    client.script = [Usage(1_000_000, 1_000_000, 2_000_000)]
    budget = CurrencyBudget(Decimal("1"), "USD", max_requests=40)
    wrapper = BudgetedClient(client, _rate(), budget)
    wrapper.begin_case()
    async for _ in wrapper.stream_chat([]):
        pass
    per_case = wrapper.case_billing()
    assert per_case is not None
    assert per_case.currency == "USD"
    assert Decimal(per_case.estimated_amount) == Decimal("0.2250")
    wrapper.begin_case()
    assert wrapper.case_billing() is None  # 复位后无增量，baseline 不能累计旧 case
