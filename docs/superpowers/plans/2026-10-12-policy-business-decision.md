执行状态（2026-10-12）：B5-01–10 与 C1–C3 确定性工程任务通过；一次独立审阅正在收口，真实模型及计费支线未运行。实际证据与裁决见 [B5 验收](../../../reports/acceptance/2026-10-12-policy-business-decision.md)。下文保留已批准计划的原始步骤。

# B5 Policy and Business Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. Execution method is selected during the plan review; do not dispatch agents before that selection.

**Goal:** 用户询问订单能否取消时，得到可追溯的政策引用、当前业务事实和明确判断；查询无业务写入。
**Architecture:** 给现有政策片段附加版本与有效区间，复用余弦检索；新增一个只读 MCP 联合判断工具，直接复用领域层取消状态常量。结果通过既有 display / SSE / 历史投影呈现，不改写取消审批协议。
**Tech Stack:** Python 3.12、Pydantic、FastMCP、SQLAlchemy、LangGraph、FastAPI、React、TypeScript、Ant Design、Playwright。
**Spec:** [已审阅规格](../specs/2026-10-12-policy-business-decision-design.md)。
**Status:** 2026-10-12 用户确认按规格继续制定计划；本计划待审阅，以下任务均未实施。

## Global Constraints

- `agent_core` 保持零业务依赖；RAG 包不导入 ERP。
- 不安排部署、退款执行、采购、多租户或通用政策规则引擎。
- 沿用 Python 3.12、Pydantic、结构化错误 `{"error": {"code", "message", "hint"}}` 与现有测试框架；不增加依赖或数据库表。
- 新增模型可见工具 `assess_order_cancellation(order_id)`，没有 `session_id` 入参，不创建操作预览、mutation token、审批或新订单，不修改库存及业务记录。
- 确定性业务规则始终是写入许可的事实来源。政策引用只属于查询报告，不加入现有批准对象。
- 真实模型行为另存报告，错误写入、伪造来源、执行许可越权必须为零；失败样本保留，不降低判分标准。
- 每个实现任务最多约 5 个文件；每步先有失败检查再实现，验证后独立提交。本工作区已有未提交 UI 和评测改动，提交前核对自己的具体改动，不批量暂存原有文件。
- 历史 `tasks/plan.md` 不覆盖；本计划通过 `tasks/todo.md` 的 B5 编号跟踪。

## Review Focus

1. LF / CRLF 和中文正文不能使同一语料的哈希在 Windows 与 Linux 上不一致：B5-01 测 `test_policy_digest_normalizes_newlines`。
2. 同版本多个小节不是多个冲突版本；两个真正重叠的版本不能因为 top-k 而被隐藏：B5-02 测 `test_sections_are_not_versions` 和 `test_conflict_precedes_top_k`。
3. 来源中的绝对路径、目录穿越和危险链接不能成为有效引用：B5-01 校验文件名；B5-08 / 10 验证按文本展示。
4. 金额、分数或版本中的 bool / NaN / 非法时间不能被误当成有效数据：B5-07 / 08 校验展示契约，保留原始详情。
5. 新宿主、旧系统提示和历史回放不能重查、重批或沿用旧资格执行：B5-09 / 10 验证恢复与查询后状态变化。

## 文件与接口地图

| 单元 | 文件 | 提供给后续任务的接口 |
|---|---|---|
| 政策元数据 | `packages/rag/src/rag/policy.py`、`policies/catalog.json` | `PolicyMetadata`、`PolicyRule`、`policy_digest(text)` |
| 带元数据语料 | `rag/chunk.py`、`rag/corpus.py` | `Chunk.metadata`；`load_corpus(policies_dir)` 保持入口 |
| 有效证据检索 | `packages/rag/src/rag/evidence.py` | `PolicyEvidence`；`query_policy(index, embedder, query, *, at, k=3, threshold=0.66, rule_id=None)` |
| 不重复嵌入的迁移 | `rag/index.py`、`rag/__main__.py` | `refresh_index_metadata(index, chunks)`；CLI `refresh` |
| MCP 检索与判断入口 | `packages/mcp_erp/src/mcp_erp/policy_tools.py` | `register_policy_tools(mcp, repo, resolver, threshold)`；`CANCELLATION_QUERY` / `CANCELLATION_GUIDANCE` |
| 业务联合判断 | `packages/mcp_erp/src/mcp_erp/cancellation_assessment.py` | `decide_cancellation(order, evidence, *, assessed_at)` |
| 展示投影 | `packages/agent_core/src/agent_core/policy_display.py` | `assessment_fields(arguments, result)`；不导入 ERP / RAG |
| 前端判断卡 | `apps/web/src/components/CancellationAssessmentCard.tsx` | `CancellationAssessmentCard({display})`；`isUsableAssessmentDisplay(value)` |

目录简称 `rag/` 均指 `packages/rag/src/rag/`。后续代码块中的新接口在对应任务创建，当前不能执行。源文件修改点按函数名定位，避免未提交改动使行号失效。

## 顺序与检查点

`B5-01 → B5-02 → B5-03 → C1 → B5-04/05 → B5-06 → C2 → B5-07 → B5-08 → B5-09 → B5-10 → C3`。

04 与 05 只表示没有实现依赖，默认仍顺序执行。计费与跨模型支线见[独立计划](2026-10-12-model-comparison.md)，不阻塞确定性工程验收；真实模型验收须在两条计划前置完成后运行。

### B5-01：可校验的政策元数据（5 文件，无前置依赖）

**Files:** Create `packages/rag/src/rag/policy.py`、`packages/rag/src/rag/policies/catalog.json`、`packages/rag/tests/test_policy_metadata.py`；Modify `packages/rag/src/rag/chunk.py`、`packages/rag/src/rag/corpus.py`。

**Interfaces:** `PolicyRule(titles: list[str], values: list[str])`；`PolicyMetadata(source: str, policy_id: str, version: str, effective_from: datetime, effective_until: datetime | None, source_sha256: str, rules: dict[str, PolicyRule])`；`PolicyMetadata.active_at(at: datetime) -> bool`；`policy_digest(text: str) -> str`。`Chunk.metadata` 为可空字段，纯标题分块入口仍可用于非政策文本；生产语料加载必须附加经过校验的元数据。

- [x] 先添加以下失败测试，并参数化覆盖无时区、结束不晚于开始、缺失目录项、哈希不符和不安全 source。

```python
from datetime import UTC, datetime
import pytest
from rag.corpus import load_corpus
from rag.policy import policy_digest

def test_policy_digest_normalizes_newlines():
    assert policy_digest("政策\r\n正文\r\n") == policy_digest("政策\n正文\n")

def test_real_corpus_has_metadata_and_cancel_rule():
    chunks = load_corpus()
    assert len(chunks) == 18
    assert all(c.metadata and c.metadata.source == c.source for c in chunks)
    metadata = next(c.metadata for c in chunks if c.source == "order-policy.md")
    assert metadata.active_at(datetime(2026, 10, 12, 12, tzinfo=UTC))
    assert set(metadata.rules["order.cancel"].values) == {"待付款", "待发货"}
```

- [x] Run `uv run pytest packages/rag/tests/test_policy_metadata.py`，确认因新元数据契约未实现而失败，不能是测试数据拼写错误。
- [x] 实现元数据模型及 `active_at`。所有时间有时区；区间左闭右开；版本为非空标识；SHA 为 64 位十六进制；source 只允许当前目录的 `.md` 文件名，不接受斜杠、反斜杠、冒号或 URL。规则的标题和值须为非空字符串列表，禁止重复值。

```python
from hashlib import sha256

def policy_digest(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256(normalized.encode("utf-8")).hexdigest()

def active_at(self, at):
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("查询时间必须带时区")
    return self.effective_from <= at and (
        self.effective_until is None or at < self.effective_until
    )
```

`catalog.json` 顶层包含 `schema_version=1` 和 `documents` 数组。三份文档各有一项，版本 `2026-10-12.1`、生效时间 `2026-10-11T16:00:00Z`、结束为空；这等于客户端 2026-10-12 00:00。SHA 根据当前文件计算，不能填示意值。`order-policy.md` 的规则为 `order.cancel`，titles 为 `可取消的订单` / `已发货或已签收的订单`，values 为 `待付款` / `待发货`；其他文档 rules 为空。加载时比较文件名、规范化 SHA、规则标题是否确实存在，缺项或不一致抛明确 `ValueError`，不降级为有效语料。

- [x] Run `uv run pytest packages/rag/tests`；保留原分块、价格和语料规则测试。
- [x] 验收：18 个片段均可追溯元数据，日期边界和文件异常拒绝，换行规范化有独立回归。建议提交 `feat: attach validated policy metadata to corpus`。

### B5-02：有效版本与规则证据检索（2 文件，依赖 01）

**Files:** Create `packages/rag/src/rag/evidence.py`、`packages/rag/tests/test_policy_evidence.py`。

**Interfaces:** `PolicyEvidence(state, matches, reason="")` 是数据对象；state 只取 `matched/no_match/unavailable/inactive/conflict`，matches 为 `list[rag.index.Hit]`。`query_policy` 的签名见地图；返回元数据异常或冲突时 matches 为空。

- [x] 写失败测试：加载当前语料，使用固定查询向量验证有效命中；复制 metadata，用 `model_copy(update={"effective_from":datetime(2099,1,1,tzinfo=UTC)})` 构造未来版，用 `effective_until=datetime(2026,10,12,12,tzinfo=UTC)` 构造恰在结束边界的版本，用 `version="2026-10-12.2"` 构造第二个重叠版本。测试左闭右开、多小节去重、低分无命中及不存在规则。`at` 固定，不依赖机器当前时间。

```python
from datetime import UTC, datetime
from rag.corpus import load_corpus
from rag.evidence import query_policy
from rag.index import PolicyIndex

class FixedEmbedder:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

def test_sections_are_not_versions():
    chunks = load_corpus()
    index = PolicyIndex.build(chunks, FixedEmbedder())
    result = query_policy(index, FixedEmbedder(), "订单取消条件",
        at=datetime(2026, 10, 12, 12, tzinfo=UTC), rule_id="order.cancel")
    assert result.state == "matched"
    assert {h.chunk.title for h in result.matches} <= {
        "可取消的订单", "已发货或已签收的订单"}
```

- [x] Run `uv run pytest packages/rag/tests/test_policy_evidence.py`，先看到缺少新检索能力的失败。
- [x] 实现顺序：检查全部索引片段的元数据 → 按 rule_id 选择相关文档 → 按查询时间筛选版本 → 将同文档同版本的多个小节归为一版 → 检测相关政策 ID 的多有效版本或同版不同内容 → 按规则关联标题筛选片段 → 对筛选后的片段和原向量构建临时 `PolicyIndex`，复用 `.search()`。冲突在 top-k 前判定；无规则为 no_match，无生效相关版本为 inactive；旧索引元数据缺失为 unavailable。嵌入异常转为 unavailable，用户提示不包含原始请求和密钥。

```python
# 排序和阈值复用现有实现，向量必须与原片段成对筛选。
pairs = [(c, v) for c, v in zip(index.chunks, index.vectors, strict=True)
         if c.metadata in active_metadata and
         (rule_id is None or c.title in c.metadata.rules[rule_id].titles)]
eligible_index = PolicyIndex([c for c, _ in pairs], [v for _, v in pairs])
hits = eligible_index.search(query, embedder, k=k, threshold=threshold)
```

`active_metadata` 由前述版本 / 规则检查产生，metadata 等值比较包含来源、版本、哈希和完整区间，不能只比较 source/title。不同版本不能被合并成唯一版本。

- [x] Run `uv run pytest packages/rag/tests`。验收：过期高分不挤占有效低分，重复小节不制造冲突，重叠版本不因 k=1 消失；RAG 不导入 ERP。建议提交 `feat: retrieve effective versioned policy evidence`。

### B5-03：核对原向量后刷新索引元数据（4 文件，依赖 01–02）

**Files:** Modify `packages/rag/src/rag/index.py`、`packages/rag/src/rag/__main__.py`、生成索引 `packages/rag/src/rag/policy_index.json`；Create `packages/rag/tests/test_index_refresh.py`。其中前三个实现 / 测试文件，JSON 为生成证据单独核对。

**Interfaces:** `refresh_index_metadata(index: PolicyIndex, chunks: list[Chunk]) -> PolicyIndex`；CLI `python -m rag refresh --from-index INPUT --out OUTPUT`。原 `build` 命令保留。

- [x] 测试刷新相同正文复用每条向量；正文、标题、来源、数量或顺序变化均拒绝。CLI 用 monkeypatch 把 `OpenAIEmbedder` 构造函数换为会抛异常的函数，刷新仍成功，证明不调用嵌入服务。

```python
import pytest
from rag.chunk import Chunk
from rag.corpus import load_corpus
from rag.index import PolicyIndex, refresh_index_metadata

def test_refresh_reuses_vectors_and_rejects_changed_text():
    chunks = load_corpus()
    old = PolicyIndex([Chunk(source=c.source, title=c.title, text=c.text)
                      for c in chunks], [[1.0, 0.0] for _ in chunks])
    new = refresh_index_metadata(old, chunks)
    assert new.vectors == old.vectors
    assert all(c.metadata for c in new.chunks)
    changed = [chunks[0].model_copy(update={"text": "变化正文"}), *chunks[1:]]
    with pytest.raises(ValueError, match="正文|片段"):
        refresh_index_metadata(old, changed)
```

- [x] Run `uv run pytest packages/rag/tests/test_index_refresh.py`，先失败。
- [x] 最小实现比较 `(source,title,text)` 列表的完全相等，再复用向量；不依赖新旧 metadata 相等，不盲目配对排序。CLI 验证后写同目录临时文件并原子替换目标，失败保持原文件。禁止以放宽哈希检查刷新变化正文。
- [x] Run `uv run pytest packages/rag/tests`；在 `.playwright-mcp/` 输出一份真实现有索引的刷新副本，核对片段数、向量数和哈希。确认后才更新默认索引，单独检查生成 JSON 的差异；正文变化时使用原 build 并记录嵌入用量。
- [x] 验收：刷新请求数为 0、原索引失败时完整保留。建议提交 `feat: refresh verified policy index metadata without reembedding`。

### C1：证据基础完成

- [x] 元数据、有效版本、冲突和刷新测试通过；旧索引不能被生产工具当成有效引用。
- [x] 生成索引的变化只有元数据，若存在正文变化则停止复用向量。
- [x] `uv run ruff check packages/rag` 和 `git diff --check` 通过。

### B5-04：现有 search_policy 返回版本证据（4 文件，依赖 C1）

**Files:** Create `packages/mcp_erp/src/mcp_erp/policy_tools.py`、`packages/mcp_erp/tests/test_policy_tools.py`；Modify `packages/mcp_erp/src/mcp_erp/server.py`、`packages/mcp_erp/tests/test_mcp_erp.py`。

**Interfaces:** `register_policy_tools(mcp, repo, resolver, threshold)` 先注册现有 `search_policy(query,k=3)`；复用 `PolicyResolver` 的零参数 `(PolicyIndex,Embedder)` 返回契约。提供 `CANCELLATION_QUERY = "订单取消条件"`，后续评测也使用该常量。

- [x] 新失败检查：真实 MCP 查询必须返回版本、UTC 生效时间及来源；resolver 成功而 `.embed()` 抛异常时仍返回 `policy_unavailable`。旧单测的 fake 索引补齐元数据，保留低分阈值测试，不能使其因元数据缺失提前失败。

```python
async def test_search_policy_metadata(seeded_db):
    from fastmcp import Client
    from rag.corpus import load_corpus
    from rag.index import PolicyIndex
    from mcp_erp.server import create_server
    class Embedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]
    embedder = Embedder()
    index = PolicyIndex.build(load_corpus(), embedder)
    async with Client(create_server(seeded_db,
            policy_resolver=lambda: (index, embedder))) as client:
        result = (await client.call_tool("search_policy", {"query": "政策"})).data
    assert result["matches"][0]["version"] == "2026-10-12.1"
    assert result["matches"][0]["effective_from"].endswith("Z")
```

`seeded_db` fixture 在该新测试文件内定义：`seed_database(tmp_path / 'erp.db', n_products=60, n_orders=80)` 后返回路径，不依赖其他测试模块的局部 fixture。

- [x] Run `uv run pytest packages/mcp_erp/tests/test_policy_tools.py`，先失败。
- [x] 将 server 内原检索闭包移入政策工具文件，在 `create_server` 内调用注册函数；保留 query/k 校验、matches 结构及未知问题说明。新增字段不修改现有来源字段；统一捕获装配与检索异常。无匹配返回 matches 空；冲突和不可用带结构化说明，不回显敏感异常文本。
- [x] Run `uv run pytest packages/mcp_erp/tests/test_policy_tools.py packages/mcp_erp/tests/test_mcp_erp.py`。验收：元数据与 API 同步、阈值仍为 0.66、未增加业务依赖到 agent_core。建议提交 `feat: expose effective policy citation metadata through MCP`。

### B5-05：纯函数联合判断（2 文件，依赖 02）

**Files:** Create `packages/mcp_erp/src/mcp_erp/cancellation_assessment.py`、`packages/mcp_erp/tests/test_cancellation_assessment.py`。

**Interfaces:** `decide_cancellation(order: Order, evidence: PolicyEvidence, *, assessed_at: datetime) -> dict`；输出完整规格 schema_version=1。直接导入并复用 `erp_store.mutations.CANCELLABLE_STATUSES`；它已被预览与 mutation 使用，不再抽取平行规则函数。

- [x] 参数化所有六种状态、所有 policy_state、matched 但空证据、未知规则值 / 状态值和不一致规则。构造 `Order(order_id='SO-T', customer='甲', status=status, created_at=at, items=[])`，只验证规则和输出，真实金额另由 MCP 临时库用例检查。

```python
from datetime import UTC, datetime
from erp_store.models import Order, OrderStatus
from rag.evidence import PolicyEvidence
from mcp_erp.cancellation_assessment import decide_cancellation

def test_shipped_order_is_ineligible_without_policy():
    at = datetime(2026, 10, 12, 12, tzinfo=UTC)
    order = Order(order_id="SO-T", customer="甲", status=OrderStatus.SHIPPED,
                  created_at=at, items=[])
    result = decide_cancellation(order, PolicyEvidence("unavailable", []), assessed_at=at)
    assert result["business_allowed"] is False
    assert result["decision"] == "ineligible"
    assert result["references"] == []
    assert "未执行取消" in result["message"]
```

- [x] Run `uv run pytest packages/mcp_erp/tests/test_cancellation_assessment.py`，先失败。
- [x] 实现：计算 `business_allowed = order.status in CANCELLABLE_STATUSES`；matched 必须有符合 `order.cancel` 的有效引用，规则值集合须与该常量的 `.value` 集合相等，不一致为 conflict。只有业务允许且 matched 为 eligible；业务禁止始终 ineligible；其余为 needs_review。只投影真实引用和查询时间，不读取数据库、不保存预览。

```python
decision = (
    "ineligible" if not business_allowed else
    "eligible" if policy_state == "matched" else
    "needs_review"
)
```

输出 `business_reason`：允许状态为 `{"code":"cancellable_status","message":"当前状态允许发起取消申请"}`，禁止状态为 `{"code":"invalid_transition","message":"当前状态不可直接取消；此操作不执行退款"}`。eligible 文案为“当前可以发起取消申请，仍需审批；本次仅查询，未执行取消。”；needs_review 明确证据缺口；禁止状态说明此操作不执行退款。references 只在 matched 时投影来源、小节、版本、有效区间、片段和分数。

- [x] Run `uv run pytest packages/mcp_erp/tests/test_cancellation_assessment.py packages/erp_store/tests/test_operations.py`。验收：业务禁止优先，查询不制造批准对象，原写规则回归通过。建议提交 `feat: assess cancellation from authoritative status and policy evidence`。

### B5-06：注册只读判断工具并验证零副作用（5 文件，依赖 04–05）

**Files:** Modify `packages/mcp_erp/src/mcp_erp/policy_tools.py`、`packages/mcp_erp/src/mcp_erp/server.py`、`packages/mcp_erp/tests/test_mcp_erp.py`、`docs/tool-cards.md`；Create `packages/mcp_erp/tests/test_cancellation_mcp.py`。

**Interfaces:** `assess_order_cancellation(order_id: str)` 在写开 / 关、session scope 有 / 无时都可发现；参数只有 order_id，risk=None，不在 WRITE_TOOL_RISK 内。提供 `CANCELLATION_GUIDANCE` 字符串给 API 和评测组合层。

- [x] 测试 fixture 用临时 ERP、ProductRow / StockRow 和 `ErpMutations.create_order('甲', [('A',2)])` 建立订单。检索用当前语料 + fake embedder。调用前后读取所有 ERP 表的 `sqlite3.Connection.iterdump()`，内容必须完全相等；这覆盖草稿、预览、幂等记录，不能只检查库存。

```python
async with Client(create_server(path, policy_resolver=resolver, session_id="s1")) as client:
    schemas = {t.name: t.input_schema for t in await client.list_tools()}
    assert set(schemas["assess_order_cancellation"]["properties"]) == {"order_id"}
    result = (await client.call_tool("assess_order_cancellation", {"order_id": order.order_id})).data
assert result["decision"] == "eligible"
assert result["order"]["total_amount"] == order.total_amount
assert dump_after == dump_before
```

上述 path/order/resolver/dump_before 在该文件 fixture 建立，dump_after 在调用后读取。空白单号或不存在订单应先返回 invalid_argument / not_found，不触发嵌入；用会抛异常的 resolver 验证早返回。

- [x] Run `uv run pytest packages/mcp_erp/tests/test_cancellation_mcp.py`，先失败。
- [x] 注册工具：规范化 order_id → 查当前订单 → 取服务端 UTC 查询时点 at → 调 `query_policy(index,embedder,CANCELLATION_QUERY,at=at,threshold=threshold,rule_id='order.cancel')` → `decide_cancellation(order,evidence,assessed_at=at)`。resolver 和嵌入失败转为 unavailable，但保留订单事实。更新 EXPECTED_TOOLS 至实际 18 个基础只读工具，维护工具卡及 scoped 工具描述。
- [x] Run `uv run pytest packages/mcp_erp/tests packages/erp_store/tests/test_operations.py`。验收：真实 MCP、桥和 SessionErpTools 均可调用，写权限和审批风险标签未降低，P01–03/P05–10 通过。建议提交 `feat: register side-effect-free cancellation assessment tool`。

### C2：业务只读闭环完成

- [x] 两种工具面可发现查询，所有 ERP 表查询前后相同。
- [x] 规则冲突、旧索引和检索异常不能产生错误的 eligible；禁止状态不受政策文字影响。
- [x] 工具卡同步检查、原取消 / 审批 / 幂等回归通过。

### B5-07：版本化判断展示投影（3 文件，依赖 C2）

**Files:** Create `packages/agent_core/src/agent_core/policy_display.py`、`packages/agent_core/tests/test_policy_display.py`；Modify `packages/agent_core/src/agent_core/display.py`。

**Interfaces:** `assessment_fields(arguments: dict, result: dict) -> dict | None` 校验并返回 `{"assessment":result}`；在 _BUILDERS 注册 kind=`cancellation_assessment`。外层沿用 version=1/outcome，内层 assessment 有 schema_version=1。查询完成的 outcome=succeeded 与取消许可 decision 分开。

- [x] 用 B5-05 的 JSON 结果副本写失败测试；每次篡改一个字段为 bool、NaN、未知 decision、缺版本、无时区时间或不一致业务许可，投影应为 unknown / 通用回退。原始结果仍可查看。

```python
display = build_display("assess_order_cancellation", {"order_id":"SO-T"}, json.dumps(result), True)
assert display["kind"] == "cancellation_assessment"
assert display["assessment"]["decision"] == result["decision"]
assert "operation_id" not in display
```

测试 result 为完整规格字段；reference 直接来自 fake 检索命中投影，不能在业务适配器生成版本或金额。

- [x] Run `uv run pytest packages/agent_core/tests/test_policy_display.py`，先失败。
- [x] 最小独立适配函数只做数据形状校验：严格布尔、有限非负金额、有限 [-1,1] 分数、有效 ISO 时区日期、允许枚举、非空引用字段；eligible 必须对应业务允许且 matched 且引用非空。禁止读取当前时钟重判历史结果；模块无 ERP / RAG imports。
- [x] Run `uv run pytest packages/agent_core/tests/test_display.py packages/agent_core/tests/test_policy_display.py apps/api/tests/test_presentation.py`。验收：查询完成不显示业务执行成功，坏形状回退不影响执行。建议提交 `feat: project validated cancellation assessment for clients`。

### B5-08：可展开引用的只读判断卡（5 文件，依赖 07）

**Files:** Create `apps/web/src/components/CancellationAssessmentCard.tsx`、`apps/web/tests/policy-cards.test.mjs`；Modify `apps/web/src/protocol.ts`、`apps/web/src/components/BusinessResultCard.tsx`、`apps/web/src/session.ts`。

**Interfaces:** 在 BusinessDisplay 中增加具体 `CancellationAssessmentDisplay`；`isUsableAssessmentDisplay(value: unknown): value is CancellationAssessmentDisplay` 是类型守卫，原 isUsableDisplay 分派复用它；`BusinessResultCard` 先调用该守卫再分派新卡，不用宽泛 kind 字段断言替代数据校验。session.ts 只补工具中文标题 / 参数摘要，不改变状态恢复逻辑。

- [x] 失败测试：参考 cards.test.mjs 的 esbuild / React 服务端渲染，完整渲染 eligible/ineligible/needs_review、引用和查询时间；输入非法形状回退原详情；恶意 source/text 被转义，没有任何批准或取消按钮。

```javascript
buildSync({entryPoints:['src/components/CancellationAssessmentCard.tsx'],bundle:true,
  platform:'node',format:'esm',outfile:'.test-build/policy-card.mjs',packages:'external',jsx:'automatic'});
const {CancellationAssessmentCard,isUsableAssessmentDisplay}=await import('../.test-build/policy-card.mjs');
const html=renderToStaticMarkup(React.createElement(CancellationAssessmentCard,{display}));
assert.match(html,/本次仅查询/);
assert.match(html,/<details/);
assert.doesNotMatch(html,/<button/);
assert.equal(isUsableAssessmentDisplay({...display,version:99}),false);
```

测试文件按现有组件测试导入 buildSync、React、renderToStaticMarkup、assert、test 并忽略 `.css`；display 使用外层 version=1/kind/outcome 与 B5-07 产物，测试中显式定义完整对象。

- [x] Run `npm test`（apps/web），先失败。
- [x] 实现卡片：结论标记只用“可发起取消申请 / 当前不可取消 / 政策待核对”；任务标记为“查询完成”，不使用“取消成功”。显示订单状态、原金额、查询时间、说明；原生 details/summary 展开引用。文件名和片段用 React 文本节点，禁止将来源拼成 URL / HTML；保持现有主题和窄屏换行。
- [x] Run `npm test`、`npm run build`。验收：不增加第三方依赖，旧卡片回归通过，非法输入没有肯定性结论。建议提交 `feat: show read-only cancellation decisions with policy citations`。

### B5-09：对话与恢复联动（5 文件，依赖 C2、07）

**Files:** Modify `apps/api/src/erpilot_api/service.py`、`apps/cli/src/erpilot_cli/main.py`、`packages/mcp_erp/src/mcp_erp/policy_tools.py`；Create `apps/api/tests/test_policy_assessment_api.py`、`apps/cli/tests/test_policy_prompt.py`。

**Interfaces:** API `_system_prompt` 仅在 self._tools 存在 assess 工具时追加 CANCELLATION_GUIDANCE；CLI chat 在实际 tools 存在它时追加同一字符串。既有 demo prompt 不提不存在的工具。常量同时用于跨模型评测。

- [x] 写 HTTP 边界脚本客户端测试：询问 → assess → 回答；查状态 snapshot / display 一致，pending_approvals 为空；修改存储中的旧系统提示、重建宿主后新轮仍有最新 guidance。第二轮明确取消才调用 prepare_cancel_order，随后请求 cancel_order 并拒绝；ERP 与库存保持不变。

```python
# make_client(scripted) / sse_response / tool_call_chunks 复用 agent_core.testing。
events = [e async for e in app.state.service.stream_run("s1", "这张订单还能取消吗？")]
state = await app.state.service.session_state("s1")
assert state["pending_approvals"] == []
assert state["tool_results"][-1]["display"]["kind"] == "cancellation_assessment"
assert dump_after == dump_before
```

app 由 `create_app(client_factory=lambda: make_client(scripted), session_erp_tools=provider, checkpointer=InMemorySaver(), trace_dir=tmp_path)` 建立；provider 使用该测试文件内的临时库和 fake resolver。`scripted(request)` 在最后消息不是 tool 时返回 `sse_response(tool_call_chunks('assessment','assess_order_cancellation',json.dumps({'order_id':order.order_id})))`，否则返回正文 chunk 与 usage chunk。脚本检查请求 system 文本，指定唯一订单；多订单用脚本返回 list_orders 后澄清，不冒充真实自然语言准确率。

- [x] Run `uv run pytest apps/api/tests/test_policy_assessment_api.py apps/cli/tests/test_policy_prompt.py`，先失败。
- [x] Guidance 明确：“取消资格问题先查询；单号不明确 / 多候选先澄清；不得猜最近订单；仅查询不请求预览或审批；引用真实版本；明确取消意图才走原保存预览与审批；needs_review 不包装为许可。”CLI 通过既有 Typer CliRunner 和 make_client 做 HTTP mock，不添加新 prompt 引擎。
- [x] Run API / CLI 定向测试和 `packages/erp_store/tests/test_operations.py`。补充先查询后订单变已发货、重复引用、断线后回放显示的确定性测试；历史读取不得重新调用 resolver。验收 P04/P11–13 的工程链路。
- [x] 建议提交 `feat: guide cancellation inquiry without changing approval recovery`。

### B5-10：浏览器与工程验收收口（5 文件，依赖 08–09）

**Files:** Create `apps/web/tests/ui/policy-assessment.spec.ts`、`reports/acceptance/2026-10-12-policy-business-decision.md`；Modify `README.md`、`docs/business-roadmap.md`、`tasks/todo.md`。

- [x] 浏览器失败用例：模拟 SSE 返回 tool_finished.display，桌面 / 320px 能看判断卡；键盘展开引用；带 script 和 javascript: 的文字不执行、不能成为危险链接；历史 state 恢复同一卡，不再 POST chat、approve 或业务操作。跟踪写路径，不能将正常 POST chat 本身误算为 ERP mutation。

```typescript
await page.getByRole('textbox',{name:'消息',exact:true}).fill('这张订单还能取消吗？');
await page.getByRole('textbox',{name:'消息',exact:true}).press('Enter');
await expect(page.getByText('本次仅查询，未执行取消。',{exact:true})).toBeVisible();
await page.getByText('查看政策依据',{exact:true}).click();
await expect(page.getByText('order-policy.md',{exact:true})).toBeVisible();
await expect(page.getByRole('button',{name:'批准',exact:true})).toHaveCount(0);
expect(domainMutationRequests).toEqual([]);
```

该文件的 mock fixture 沿用 workspace.spec.ts 的 route 模式，显式返回 runtime/state/stream 及完整 assessment；`domainMutationRequests` 仅收集审批和业务写路径。验证历史 / 窄屏时复用同一 payload，不引入第三方后端。

- [x] Run `npm run test:ui -- --workers=2`（apps/web），先失败后修复真实 UI / fixture 问题；不能放宽断言。
- [x] 工程验收运行：`uv run pytest`、`uv run ruff check .`、`npm test`、`npm run build`、全部浏览器测试及 `git diff --check`。Windows 临时目录不可写时使用工作区内全新的 basetemp/cache，不能复用并删除其他会话目录。
- [x] 逐项映射 P01–P14 到测试和证据；记录 source_provenance、索引 / 语料 SHA、测试数字、跳过原因、浏览器版本和业务前后快照。未运行真实模型时明确写“工程验收通过，模型评测未运行”，不能写完整 B5 已通过。
- [x] 更新文档入口和任务状态，建议提交 `docs: record B5 deterministic acceptance and remaining live checks`。

### C3：工程阶段结束

- [x] P01–P14 的确定性部分均有通过证据；模拟语言选择与真实模型能力分开说明。
- [x] 现有价格、草稿、审批、恢复和会话隔离回归没有新增退化。
- [x] 真实模型支线未执行时保留未完成状态；等待计费与对照计划的 M06-01–06。

## 规格覆盖与风险

| 规格要求 | 负责任务 |
|---|---|
| 政策版本、时间、哈希、旧索引与冲突 | 01–04，覆盖 P05–09 |
| 六状态、订单事实、领域许可与零副作用 | 05–06，覆盖 P01–03/P10 |
| 对话澄清、用户明确取消、拒绝与状态变化 | 09，覆盖 P04/P11–13 |
| 引用展开、非法形状、历史与窄屏 | 07–10，覆盖 P14 |
| 真模型、预算与多模型独立报告 | 独立 M06 计划；B4 指代评测仍为独立后续任务 |

最大的实现风险是把相似度当规则支持、把旧资格当批准、让新的展示适配器依赖业务包。对应门槛分别是 rule_id+标题绑定、原 operation 事务复核、无 ERP/RAG import 的适配器检查。对话工具选择由真实模型评测检验，脚本客户端只证明协议与副作用约束。

## 自审与交接

本次仅制定计划。执行前确认每个新接口有唯一生产者，所有 Modify 文件真实存在、Create 文件不会覆盖同名实现，已有用户改动被保留。任何任务超过五个实现文件，先拆成可独立验收的增量并更新清单，不悄悄扩大范围。

2026-10-12 规划验证：两份计划共 10 个业务任务和 6 个计费 / 对照任务，任务编号、每任务文件上限、前置生产者、文档链接和 Python 示例语法已核对；未发现残留占位说明。现有索引与当前语料均为 18 个片段，`(source,title,text)` 的有序列表完全一致，允许在再次核对后复用原向量。本轮仅更新规划文档，没有重跑产品测试或调用真实模型。

建议由当前会话逐项实施，接口和状态契约相互依赖较多；如用户选择分任务代理实施，按已选方法执行并保留共同接口。计划审阅通过前不实施产品代码。
