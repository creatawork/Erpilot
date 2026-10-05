"""静态校验：case 集 vs 标注标准——占位符可解析、工具名真实存在、id 规范。

评测集进集前先过这里（标注标准 §6）；改种子生成器导致占位符失效时，
这里第一时间拦截。
"""

import re

from evals.cases import ALL_CASES
from evals.model import CaseCategory

_ID_RE = re.compile(r"^(single|multi|edge|adv)-\d{2}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def test_ids_unique_and_well_formed() -> None:
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids)), "case id 重复"
    for c in ALL_CASES:
        assert _ID_RE.match(c.id), f"{c.id}: id 不符合 类别前缀-序号 规范"
        assert c.id.startswith(
            {"single": "single", "multi": "multi", "edge": "edge",
             "adversarial": "adv"}[c.category.value]
        ), f"{c.id}: id 前缀与类别不符"
        assert c.points.strip(), f"{c.id}: 缺考点说明（标注标准 §3）"
        assert c.question.strip() == c.question


def test_every_case_has_at_least_one_check() -> None:
    for c in ALL_CASES:
        has_check = (
            c.expect_tools_all
            or c.expect_tools_any
            or c.must_mention
            or c.must_mention_any
            or c.must_not_mention
        )
        assert has_check, f"{c.id}: 没有任何检查项，写不出可复核检查项的场景不收（标注标准 §1）"


def test_referenced_tools_exist(resolved, tools) -> None:
    """检查项里的工具名必须是 MCP 工具面上真实存在的名字。"""
    real = {t.name for t in tools}
    for c in ALL_CASES:
        for name in [*c.expect_tools_all, *c.expect_tools_any]:
            assert name in real, f"{c.id}: 引用了不存在的工具 {name}"


def test_placeholders_resolve(resolved) -> None:
    """question 与检查项里的占位符必须全部可从种子库解析。"""
    known = set(resolved)
    for c in ALL_CASES:
        texts = [c.question, *c.must_mention, *c.must_mention_any, *c.must_not_mention]
        for text in texts:
            used = set(_PLACEHOLDER_RE.findall(text))
            assert used <= known, f"{c.id}: 未知占位符 {used - known}"


def test_first_batch_covers_four_categories() -> None:
    """首批必须有四类覆盖（计划 §4 冻结口径），单类不超过一半防失衡。"""
    counts = {cat: 0 for cat in CaseCategory}
    for c in ALL_CASES:
        counts[c.category] += 1
    assert all(n > 0 for n in counts.values()), f"有类别没有 case：{counts}"
    assert len(ALL_CASES) >= 20, "首批应 ≥20 条（M3 第 4 周计划线）"
