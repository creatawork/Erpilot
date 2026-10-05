"""静态校验：case 集 vs 标注标准——占位符可解析、工具名真实存在、id 规范。

评测集进集前先过这里（标注标准 §6）；改种子生成器导致占位符失效时，
这里第一时间拦截。读评测集（ALL_CASES）与写评测集（WRITE_CASES）共用
同一套规则；写集额外要求 id 不与读集冲突。
"""

import re

import pytest
from evals.cases import ALL_CASES
from evals.model import CaseCategory
from evals.write_cases import WRITE_CASES, WRITE_ERROR_CASES

_ID_RE = re.compile(r"^(single|multi|edge|adv)-\d{2}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

_CATEGORY_PREFIX = {
    "single": "single",
    "multi": "multi",
    "edge": "edge",
    "adversarial": "adv",
}


def test_ids_unique_and_well_formed() -> None:
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids)), "case id 重复"
    for c in ALL_CASES:
        assert _ID_RE.match(c.id), f"{c.id}: id 不符合 类别前缀-序号 规范"
        assert c.id.startswith(
            {"single": "single", "multi": "multi", "edge": "edge", "adversarial": "adv"}[
                c.category.value
            ]
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
            or c.must_mention_any_groups
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
        texts = [
            c.question, *c.must_mention, *c.must_mention_any, *c.must_not_mention,
            *(item for group in c.must_mention_any_groups for item in group),
        ]
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


# ---- 写操作类评测集（M4 第 1 周，标注标准 §7） ----


def test_write_cases_follow_annotation_rules(resolved, write_tools) -> None:
    """写集与读集同一套规则：id 规范不冲突、考点必填、至少一个检查项、
    占位符可解析；引用的工具名必须在写工具面上真实存在。"""
    real = {t.name for t in write_tools}
    read_ids = {c.id for c in ALL_CASES}
    for c in WRITE_CASES:
        assert _ID_RE.match(c.id), f"{c.id}: id 不符合 类别前缀-序号 规范"
        assert c.id not in read_ids, f"{c.id}: 与读评测集 id 冲突（id 永不复用）"
        assert c.id.startswith(_CATEGORY_PREFIX[c.category.value])
        assert c.points.strip(), f"{c.id}: 缺考点说明"
        has_check = (
            c.expect_tools_all
            or c.expect_tools_any
            or c.must_mention
            or c.must_mention_any
            or c.must_not_mention
        )
        assert has_check, f"{c.id}: 没有任何检查项"
        for name in [*c.expect_tools_all, *c.expect_tools_any]:
            assert name in real, f"{c.id}: 引用了写工具面上不存在的工具 {name}"
        texts = [c.question, *c.must_mention, *c.must_mention_any, *c.must_not_mention]
        used = set(_PLACEHOLDER_RE.findall(" ".join(texts)))
        assert used <= set(resolved), f"{c.id}: 未知占位符 {used - set(resolved)}"


def test_write_cases_are_all_governance_adversarial() -> None:
    """第一周写集全部落 adversarial（治理行为），且必须防假装执行。"""
    assert WRITE_CASES, "写评测集不应为空"
    assert all(c.category is CaseCategory.ADVERSARIAL for c in WRITE_CASES), (
        "M4 第 1 周写 case 只考'未批准不得假装执行'（标注标准 §7）"
    )
    for c in WRITE_CASES:
        assert any(
            "执行" in s or "审批" in s for s in [*c.must_mention_any, *c.must_mention, *c.points]
        ), f"{c.id}: 写 case 必须把'未执行/需审批'口径写进检查项或考点"


# ---- 写路径错误自愈观察集（T02，标注标准 §7.2） ----


def test_write_error_cases_follow_annotation_rules(resolved, write_tools) -> None:
    """观察集与写集同一套规则：id 规范不冲突、考点必填、占位符可解析、
    引用的工具名在写工具面上真实存在。"""
    real = {t.name for t in write_tools}
    taken = {c.id for c in ALL_CASES} | {c.id for c in WRITE_CASES}
    for c in WRITE_ERROR_CASES:
        assert _ID_RE.match(c.id), f"{c.id}: id 不符合 类别前缀-序号 规范"
        assert c.id not in taken, f"{c.id}: 与既有评测集 id 冲突（id 永不复用）"
        assert c.id.startswith(_CATEGORY_PREFIX[c.category.value])
        assert c.points.strip(), f"{c.id}: 缺考点说明"
        has_check = (
            c.expect_tools_all
            or c.expect_tools_any
            or c.must_mention
            or c.must_mention_any
            or c.must_not_mention
        )
        assert has_check, f"{c.id}: 没有任何检查项"
        for name in [*c.expect_tools_all, *c.expect_tools_any]:
            assert name in real, f"{c.id}: 引用了写工具面上不存在的工具 {name}"
        texts = [
            c.question, *c.must_mention_any, *c.must_mention, *c.must_not_mention,
            *(item for group in c.must_mention_any_groups for item in group),
        ]
        used = set(_PLACEHOLDER_RE.findall(" ".join(texts)))
        assert used <= set(resolved), f"{c.id}: 未知占位符 {used - set(resolved)}"


def test_write_error_cases_expect_zero_business_change() -> None:
    """业务失败的观察 case 必须带 unchanged 状态期望——拒绝/失败零业务变更。"""
    assert WRITE_ERROR_CASES, "写错误观察集不应为空"
    for c in WRITE_ERROR_CASES:
        assert c.state is not None and c.state.kind == "unchanged", (
            f"{c.id}: 业务失败场景必须期望业务四表零变更（标注标准 §7.2）"
        )
        assert c.expect_error_codes, f"{c.id}: 必须实际观测业务错误码"


def test_write_error_scenarios_trigger_on_seed(seeded_db, resolved) -> None:
    """对照种子数据验证两类业务错误确实可触发（占位符场景非纸面设计）。

    触发调用发生在业务校验层、先于任何落盘，共享种子库不被污染。
    """
    from erp_store.db import make_engine
    from erp_store.models import ProductStatus
    from erp_store.mutations import ErpMutations, MutationError

    mutations = ErpMutations(make_engine(seeded_db))
    with pytest.raises(MutationError) as insufficient:
        mutations.adjust_stock(resolved["zero_stock_sku"], -5)
    assert insufficient.value.code == "insufficient_stock"
    assert insufficient.value.hint, "insufficient_stock 必须带 hint（自愈线索）"

    with pytest.raises(MutationError) as transition:
        mutations.set_product_status(resolved["on_sale_sku"], ProductStatus.ON_SALE)
    assert transition.value.code == "invalid_transition"
    assert transition.value.hint, "invalid_transition 必须带 hint（自愈线索）"


# ---- 脚本化审批策略评测集（M4 第 2 周，标注标准 §7 批准确率起步） ----

from evals.approval_cases import APPROVAL_CASES  # noqa: E402

_APPROVAL_ID_RE = re.compile(r"^app-\d{2}$")


def test_approval_cases_follow_annotation_rules(resolved, write_tools) -> None:
    """策略集：app- 前缀 id 不与读/写集冲突、占位符可解析、工具名真实存在。"""
    real = {t.name for t in write_tools}
    taken = {c.id for c in ALL_CASES} | {c.id for c in WRITE_CASES}
    for c in APPROVAL_CASES:
        assert _APPROVAL_ID_RE.match(c.id), f"{c.id}: 策略集 id 须为 app-序号"
        assert c.id not in taken, f"{c.id}: 与既有评测集 id 冲突（id 永不复用）"
        assert c.points.strip(), f"{c.id}: 缺考点说明"
        has_check = (
            c.expect_tools_all
            or c.expect_tools_any
            or c.must_mention
            or c.must_mention_any
            or c.must_not_mention
        )
        assert has_check, f"{c.id}: 没有任何检查项"
        for name in [*c.expect_tools_all, *c.expect_tools_any]:
            assert name in real, f"{c.id}: 引用了写工具面上不存在的工具 {name}"
        texts = [c.question, *c.must_mention, *c.must_mention_any, *c.must_not_mention]
        used = set(_PLACEHOLDER_RE.findall(" ".join(texts)))
        assert used <= set(resolved), f"{c.id}: 未知占位符 {used - set(resolved)}"


def test_approval_cases_cover_both_paths() -> None:
    """批准确率的两个方向都要有 case：放行路径钉住执行，拒绝路径防假装。"""
    approved = [c for c in APPROVAL_CASES if c.expect_tools_all]
    denied = [c for c in APPROVAL_CASES if not c.expect_tools_all]
    assert approved and denied, "策略集须同时覆盖批准路径与拒绝路径"
    for c in approved:  # 放行路径必须排除"未执行"措辞（防把执行说成没执行）
        assert any("未执行" in s or "无法执行" in s for s in c.must_not_mention), c.id
    for c in denied:  # 拒绝路径必须把"未执行/需审批"口径写进检查项
        assert c.must_mention_any, c.id
