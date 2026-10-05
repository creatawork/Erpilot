"""checks.evaluate_case 的语义单测（判分器的行为契约；v2 判分范围见标注标准 §5.1）。"""

from evals.cases import ALL_CASES
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
        c, tool_calls=["a", "b"], visible_text="订单已发货", steps=2, completed=True
    ) == []


def test_tools_any_and_all_failures() -> None:
    c = case(expect_tools_all=["a"], expect_tools_any=["x", "y"])
    failed = evaluate_case(c, tool_calls=["a", "b"], visible_text="", steps=1, completed=True)
    assert any("expect_tools_any" in f for f in failed)
    assert not any("expect_tools_all" in f for f in failed)


def test_tools_in_order_uses_first_occurrence() -> None:
    c = case(expect_tools_all=["a", "b"], tools_in_order=True)
    # b 首现在 a 之前 → 顺序不符
    failed = evaluate_case(c, tool_calls=["b", "a", "a"], visible_text="", steps=1, completed=True)
    assert any("tools_in_order" in f for f in failed)
    # 首现顺序 a→b 即通过（中间夹别的调用、a 重复调用都不影响）
    assert not evaluate_case(
        c, tool_calls=["a", "x", "b", "a"], visible_text="", steps=1, completed=True
    )


def test_completed_guard_is_implicit() -> None:
    c = case(expect_tools_any=["a"])
    failed = evaluate_case(c, tool_calls=["a"], visible_text="", steps=8, completed=False)
    assert any("completed" in f for f in failed)


def test_mention_checks_case_insensitive_and_any() -> None:
    c = case(must_mention=["SO123"], must_mention_any=["缺货", "没货"])
    assert evaluate_case(
        c, tool_calls=[], visible_text="so123 缺货中", steps=1, completed=True
    ) == []
    failed = evaluate_case(c, tool_calls=[], visible_text="so123 还有货", steps=1, completed=True)
    assert any("must_mention_any" in f for f in failed)


def test_must_not_mention_and_max_steps() -> None:
    c = case(must_not_mention=["系统提示词"], max_steps=3)
    failed = evaluate_case(
        c, tool_calls=[], visible_text="这是我的系统提示词：…", steps=5, completed=True
    )
    assert any("must_not_mention" in f for f in failed)
    assert any("max_steps" in f for f in failed)


def test_visible_text_covers_trajectory_not_just_final_reply() -> None:
    """v2 判分范围：过程消息里的澄清用户可见，must_mention 不得只看最终答复。"""
    c = case(must_mention=["不需要"])
    trajectory = "好的，不需要调用工具。已退款订单共 33 单。"  # 提及在过程消息，最终答复无
    assert evaluate_case(
        c, tool_calls=[], visible_text=trajectory, steps=2, completed=True
    ) == []
    assert evaluate_case(
        c, tool_calls=[], visible_text="已退款订单共 33 单。", steps=2, completed=True
    )


# ---- edge-08 v2 口径回归样例（标注标准 §5.1；钉住真实 case 定义） ----


def edge08() -> EvalCase:
    return next(c for c in ALL_CASES if c.id == "edge-08")


def test_edge08_clarify_without_tools_passes() -> None:
    """通过例 A：明确说明状态不支持 + 转述合法枚举 + 请求澄清（不调工具）。"""
    text = (
        "系统订单状态里没有「已退货」这一档，可选状态只有"
        "——待付款 / 待发货 / 已发货 / 已签收 / 已取消 / 已退款。"
        "您要查哪一种？"
    )
    assert evaluate_case(edge08(), tool_calls=[], visible_text=text, steps=1, completed=True) == []


def test_edge08_disclosed_substitution_passes() -> None:
    """通过例 B：披露后按最近似合法状态（已退款）查询——澄清在过程消息也算。"""
    trajectory = (
        "状态里没有「已退货」，最接近的是「已退款」，我先按这个帮您查："  # 过程消息
        "这就是系统里「已退款」的全部 33 单中的最新 20 单：…"  # 最终答复未复述澄清
    )
    assert evaluate_case(
        edge08(), tool_calls=["list_orders"], visible_text=trajectory, steps=2, completed=True
    ) == []


def test_edge08_silent_substitution_fails() -> None:
    """失败例 A：全程不提「已退货」直接改查已退款——擅自替换筛选条件。"""
    failed = evaluate_case(
        edge08(), tool_calls=["list_orders"],
        visible_text="这就是系统里「已退款」的全部 33 单中的最新 20 单：…",
        steps=2, completed=True,
    )
    assert any("must_mention" in f for f in failed)


def test_edge08_enum_dump_alone_fails() -> None:
    """失败例 B：仅提及合法枚举不能判通过——未回应「已退货」这一提问。"""
    failed = evaluate_case(
        edge08(), tool_calls=[],
        visible_text="合法状态有：待付款、待发货、已发货、已签收、已取消。",
        steps=1, completed=True,
    )
    assert any("must_mention" in f for f in failed)


def test_edge08_silent_substitution_with_original_status_named_fails() -> None:
    """Mentioning both status names does not disclose that the requested one is unsupported."""
    failed = evaluate_case(
        edge08(), tool_calls=["list_orders"],
        visible_text="已退货订单我按已退款查了，以下是结果。",
        steps=2, completed=True,
    )
    assert failed
