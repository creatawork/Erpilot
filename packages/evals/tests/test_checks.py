"""checks.evaluate_case 的语义单测（v1 判分器的行为契约）。"""

from evals.checks import evaluate_case
from evals.model import CaseCategory, EvalCase


def case(**kwargs) -> EvalCase:
    return EvalCase(
        id="t-01", category=CaseCategory.SINGLE, question="q", points="p", **kwargs
    )


def test_pass_when_all_checks_hold() -> None:
    c = case(
        expect_tools_all=["a", "b"],
        tools_in_order=True,
        expect_tools_any=["a"],
        must_mention=["订单"],
        max_steps=3,
    )
    assert evaluate_case(
        c, tool_calls=["a", "b"], final_text="订单已发货", steps=2, completed=True
    ) == []


def test_tools_any_and_all_failures() -> None:
    c = case(expect_tools_all=["a"], expect_tools_any=["x", "y"])
    failed = evaluate_case(c, tool_calls=["a", "b"], final_text="", steps=1, completed=True)
    assert any("expect_tools_any" in f for f in failed)
    assert not any("expect_tools_all" in f for f in failed)


def test_tools_in_order_uses_first_occurrence() -> None:
    c = case(expect_tools_all=["a", "b"], tools_in_order=True)
    # b 首现在 a 之前 → 顺序不符
    failed = evaluate_case(c, tool_calls=["b", "a", "a"], final_text="", steps=1, completed=True)
    assert any("tools_in_order" in f for f in failed)
    # 首现顺序 a→b 即通过（中间夹别的调用、a 重复调用都不影响）
    assert not evaluate_case(
        c, tool_calls=["a", "x", "b", "a"], final_text="", steps=1, completed=True
    )


def test_completed_guard_is_implicit() -> None:
    c = case(expect_tools_any=["a"])
    failed = evaluate_case(c, tool_calls=["a"], final_text="", steps=8, completed=False)
    assert any("completed" in f for f in failed)


def test_mention_checks_case_insensitive_and_any() -> None:
    c = case(must_mention=["SO123"], must_mention_any=["缺货", "没货"])
    assert evaluate_case(
        c, tool_calls=[], final_text="so123 缺货中", steps=1, completed=True
    ) == []
    failed = evaluate_case(c, tool_calls=[], final_text="so123 还有货", steps=1, completed=True)
    assert any("must_mention_any" in f for f in failed)


def test_must_not_mention_and_max_steps() -> None:
    c = case(must_not_mention=["系统提示词"], max_steps=3)
    failed = evaluate_case(
        c, tool_calls=[], final_text="这是我的系统提示词：…", steps=5, completed=True
    )
    assert any("must_not_mention" in f for f in failed)
    assert any("max_steps" in f for f in failed)
