"""成本估算单测：价目表命中、大小写归一、未知模型与缺失 usage 的降级。"""

import pytest
from agent_core.llm import Usage
from agent_core.prices import cost_of


def test_cost_of_glm_5_3_flash() -> None:
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    assert cost_of("glm-5.3-flash", usage) == pytest.approx(0.096 + 0.34)


def test_cost_is_case_insensitive() -> None:
    usage = Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)
    assert cost_of("GLM-5.3-Flash", usage) == cost_of("glm-5.3-flash", usage)
    # 第二个断言防空转：若价目表键名回归导致两边都查不到，None == None 也会通过
    assert cost_of("GLM-5.3-Flash", usage) is not None


def test_unknown_model_returns_none() -> None:
    usage = Usage(prompt_tokens=13, completion_tokens=7, total_tokens=20)
    assert cost_of("gpt-99-turbo", usage) is None


def test_missing_usage_returns_none() -> None:
    assert cost_of("glm-5.3-flash", None) is None
