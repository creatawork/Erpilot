# Crash Recovery and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely recover approved ERP writes after refresh, disconnect, or process crash, and prove the behavior for all four write tools across R01–R10.

**Architecture:** Keep LangGraph PostgreSQL checkpoints as the execution-state source, ERP SQLite mutation idempotency rows as the commit/result source, and API RunStore as a display projection. Add an API-injected mutation reconciler, persist approval expiry and invocation outcomes in graph state, serialize session recovery/cancel in the supported single API process, and replay presentation events from a durable sequence cursor.

**Tech Stack:** Python 3.12, LangGraph, PostgreSQL AsyncPostgresSaver, SQLite/SQLAlchemy, FastAPI/SSE, React/TypeScript, pytest, temporary databases and subprocess crash workers.

**Spec:** [Crash Recovery and Reconciliation Design](../specs/2026-10-08-crash-recovery-reconciliation-design.md) and [ADR-0010](../../adr/0010-crash-recovery-reconciliation-scope.md)

## Global Constraints

- Support one trusted local user and one API execution process; do not claim multi-worker or distributed execution ownership.
- PostgreSQL graph checkpoint is the execution-state authority; `MutationRequestRow` is the only business commit/result authority; RunStore remains a display projection.
- Persist normalized arguments, tool schema version, argument fingerprint, original client token, pending ID, approval timestamps, invocation status and structured result before crossing the relevant side-effect boundary.
- Never create a new token while recovering the same invocation; query failure, token conflict, or incompatible schema must fail closed.
- Approval TTL defaults to 1800 seconds in UTC and is not extended by refresh, reconnect, or restart; a persisted approval is not revoked by the pending TTL.
- SSE disconnect only stops the subscriber. Explicit cancellation cannot roll back a committed ERP mutation.
- Keep `agent_core` independent of `erp_store` and `mcp_erp`; inject only a generic reconciliation interface into the graph runtime.
- Preserve existing chat, approval, resume, SSE event, and `{ok: boolean}` compatibility contracts; additions are additive.
- Use only temporary ERP databases and isolated PostgreSQL schemas for destructive tests; do not call real-model evaluations in this plan.

## Review Focus

1. **Checkpoint committed, ERP write absent:** an approved invocation may retry once with the same token only after a successful absent lookup. Pin this in Task 3's `test_absent_token_retries_original_call_once` and Task 7's R04 cases.
2. **ERP committed, graph result absent:** recovery must return the stored first result without another business mutation. Pin this in Task 3's `test_found_token_returns_first_result_without_handler` and Task 7's R05 cases.
3. **ERP lookup unavailable or token/request mismatch:** recovery must not invoke a write handler and must expose `unknown`. Pin this in Task 3's `test_lookup_failure_and_conflict_fail_closed`.
4. **Approval expires during page refresh or process downtime:** original expiration must remain fixed and expired decisions must never execute a write. Pin this in Task 1's `test_approval_expiry_survives_checkpoint_reload` and Task 7's R02 cases.
5. **Resume races with cancel or event replay:** at most one session action owns execution, and a snapshot/event race cannot lose or duplicate a user-visible event. Pin this in Tasks 4–6's concurrency and cursor tests.

---

## File Map

| File | Responsibility |
|---|---|
| `packages/erp_store/src/erp_store/mutations.py` | Canonical mutation request construction and read-only token/result lookup |
| `packages/erp_store/tests/test_mutations.py` | Found, absent, conflict and transaction-safe lookup behavior |
| `packages/mcp_erp/src/mcp_erp/bridge.py` | Construct the API-injectable reconciler against the same ERP database and tool schemas |
| `packages/mcp_erp/tests/test_mcp_erp.py` | Reconciler adapter contract with all four write tools |
| `packages/agent_core/src/agent_core/graph_state.py` | Serializable approval lifecycle and per-invocation recovery fields |
| `packages/agent_core/src/agent_core/graph_approval.py` | Approval payload, TTL metadata and decision validation |
| `packages/agent_core/src/agent_core/graph.py` | Reconcile-before-write behavior and terminal outcome routing |
| `packages/agent_core/src/agent_core/graph_runtime.py` | Generic reconciliation injection, checkpoint state reads and resume operations |
| `packages/agent_core/tests/test_graph_approval.py`, `test_graph_tools.py`, `test_graph_persistence.py` | Deterministic graph lifecycle, token reuse and checkpoint recovery tests |
| `apps/api/src/erpilot_api/recovery.py` | API-layer `MutationReconciler` adapter and recovery outcome mapping |
| `apps/api/src/erpilot_api/service.py` | Session ownership, approval expiry, cancel/resume serialization and snapshot assembly |
| `apps/api/src/erpilot_api/run_store.py` | Atomic per-run event sequence allocation and replay queries; display-only data |
| `apps/api/src/erpilot_api/main.py` | Add cancel and event replay endpoints while preserving existing routes |
| `apps/api/tests/test_recovery.py`, `test_api.py`, `test_run_store.py` | Recovery service, API concurrency, event cursor and compatibility tests |
| `apps/web/src/protocol.ts`, `session.ts`, `App.tsx`, `components/WorkTimeline.tsx` | Typed `unknown`/expired state, snapshot bootstrap and cursor-based event hydration |
| `apps/web/tests/protocol.test.mjs`, `session.test.mjs` | API parsing, event de-duplication and visible recovery states |
| `apps/api/tests/fault_worker.py`, `test_process_recovery.py` | Child process crash points and isolated PostgreSQL/ERP recovery matrix |
| `docs/adr/0010-crash-recovery-reconciliation-scope.md`, `docs/superpowers/specs/2026-10-08-crash-recovery-reconciliation-design.md` | Accepted decision and implementation contract after review |
| `reports/acceptance/` | Machine-readable and human-readable R01–R10 evidence |

## Task 1: Persist Approval Lifecycle Metadata

**Files:**
- Modify: `packages/agent_core/src/agent_core/graph_state.py`
- Modify: `packages/agent_core/src/agent_core/graph_approval.py`
- Modify: `packages/agent_core/src/agent_core/graph_tools.py`
- Modify: `packages/agent_core/src/agent_core/graph.py`
- Modify: `packages/agent_core/src/agent_core/events.py`
- Test: `packages/agent_core/tests/test_graph_approval.py`
- Test: `packages/agent_core/tests/test_graph_tools.py`
- Test: `packages/agent_core/tests/test_graph_persistence.py`
- Test: `packages/agent_core/tests/test_graph_runtime.py`

**Interfaces:**
- `prepare_tool_calls(calls, tools, *, now, approval_ttl_seconds)` produces normalized write calls with `tool_schema_version`, `arguments_fingerprint`, `client_token`, `pending_id`, `approval_created_at`, `approval_expires_at`, `approval_status="pending"`, and `invocation_status="waiting_approval"`.
- Approval resume accepts one of `approved`, `denied`, `expired`, or `cancelled`; only `approved` can reach the write handler.
- A test clock is injected into preparation; production uses timezone-aware UTC.

- [x] **Step 1: Add failing lifecycle tests** for UTC expiry calculation, unchanged expiry after checkpoint reload, expiry propagation through `ApprovalPending`, wrong pending ID, and expired/denied/cancelled decisions never calling the handler.
- [x] **Step 2: Run the new lifecycle/runtime tests before implementation** and confirm the expected failures: missing `now`/TTL support, inconsistent outcome validation, and `ApprovalPending` rejecting `expires_at`.
- [x] **Step 3: Add serializable lifecycle fields and validate the expiry/pending/token binding before interrupt resume.** Keep checkpoint state limited to JSON/MsgPack-compatible values.
- [x] **Step 4: Route `expired`, `denied`, and `cancelled` decisions to structured terminal results without entering `execute_tool_call`.** Preserve the existing approved path.
- [x] **Step 5: Rerun `uv run pytest packages/agent_core/tests -q` and `uv run ruff check packages/agent_core`**; confirm old graph approval tests remain unchanged.
- [x] **Step 6: Commit the lifecycle state slice** with message `feat: persist approval recovery lifecycle`.

## Task 2: Add a Canonical Read-Only Mutation Lookup

**Files:**
- Modify: `packages/erp_store/src/erp_store/mutations.py`
- Test: `packages/erp_store/tests/test_mutations.py`

**Interfaces:**
- `ErpMutations.lookup_result(tool_name: str, arguments: dict[str, object], client_token: str) -> tuple[str, object | None]` returns `("found", result)`, `("absent", None)`, or `("conflict", None)`.
- The request normalizer used by lookup is the same helper used by `create_order`, `cancel_order`, `adjust_stock`, and `set_product_status`; it excludes `client_token` from the business request body.
- Database/serialization errors raise to the caller and are never converted to `absent`.

- [x] **Step 1: Add failing tests** that perform each mutation, look up its token/result, look up a missing token, supply the same token with changed arguments, and prove lookup does not change business rows or insert a mutation record.
- [x] **Step 2: Run `uv run pytest packages/erp_store/tests/test_mutations.py -q`** and confirm the new lookup API is missing.
- [x] **Step 3: Extract one canonical request builder per mutation** and use it from both the write operation and `lookup_result`; compare the stored `MutationRequestRow.request` exactly.
- [x] **Step 4: Implement a read-only session lookup** that returns found/absent/conflict and lets all database failures propagate.
- [x] **Step 5: Rerun `uv run pytest packages/erp_store/tests -q` and `uv run ruff check packages/erp_store/src/erp_store/mutations.py packages/erp_store/tests/test_mutations.py`**; inspect that each write transaction still stores mutation and result atomically.
- [x] **Step 6: Commit the store contract** with message `feat: expose mutation token reconciliation lookup`.

## Task 3: Reconcile Before Every Recovered Write

**Files:**
- Modify: `packages/mcp_erp/src/mcp_erp/bridge.py`
- Modify: `packages/agent_core/src/agent_core/graph_runtime.py`
- Modify: `packages/agent_core/src/agent_core/graph.py`
- Create: `apps/api/src/erpilot_api/recovery.py`
- Test: `packages/mcp_erp/tests/test_mcp_erp.py`
- Test: `packages/agent_core/tests/test_graph_tools.py`
- Test: `packages/agent_core/tests/test_graph_persistence.py`
- Test: `apps/api/tests/test_recovery.py`

**Interfaces:**
- `MutationLookup` is an immutable generic value with `status: Literal["found", "absent", "conflict"]` and optional `result`.
- `MutationReconciler.lookup(tool_name, arguments, client_token) -> MutationLookup` is an async protocol injected into `LangGraphRuntime`; `agent_core` imports no ERP/MCP modules.
- `ErpMutationReconciler` uses the same ERP `db_path`, calls `ErpMutations.lookup_result` in a worker thread, and maps database failures to `unknown` without invoking a handler.

- [x] **Step 1: Add failing graph tests** for found result/no handler, absent result/one call with original token, conflict/no handler, lookup exception/no handler, unsupported tool schema version/no handler, and repeated explicit resume after unknown.
- [x] **Step 2: Run focused graph and API tests**; confirmed the reconciler injection was missing before implementation.
- [x] **Step 3: Add the generic reconciler protocol and API adapter.** ERP imports remain in MCP/API composition code.
- [x] **Step 4: Update the graph write node to query before handler execution.** `found` reuses the stored result; `absent` uses the original arguments/token once; conflict, query failure, and schema mismatch persist `unknown` and stop model continuation. Unknown exposes an explicit retry interrupt that re-queries the same token.
- [x] **Step 5: Add tests proving found results bypass the handler, absence invokes once with the original token, all four ERP response shapes map correctly, and repeated explicit lookup recovers without a write.**
- [x] **Step 6: Run focused graph/MCP/API suites and Ruff**; commit as `feat: reconcile checkpointed writes by token`.

## Task 4: Serialize Resume, Approval, Expiry, and Cancellation

**Files:**
- Modify: `apps/api/src/erpilot_api/service.py`
- Modify: `apps/api/src/erpilot_api/main.py`
- Test: `apps/api/tests/test_api.py`
- Test: `apps/api/tests/test_recovery.py`

**Interfaces:**
- All operations that can change a session checkpoint use the existing per-session lock and re-read checkpoint state after acquiring it.
- Add `POST /api/sessions/{session_id}/cancel`; idempotent cancellation of pending work persists `cancelled`, and a currently active owner returns `409 session_busy` without interrupting a handler.
- Expired approval returns `410 approval_expired`; it cannot be translated into an approval decision with `approved=True`.
- Existing `/api/chat/approve`, `/api/chat/approve/stream`, `/api/sessions/{session_id}/resume/stream`, and status/error shapes remain compatible.

- [x] **Step 1: Add failing API tests** for expired approval, late conflicting approval, active-session cancel conflict, cancel-before-resume and idempotent repeated cancel.
- [x] **Step 2: Run focused API/graph tests**; confirmed expiry finalization and cancel route were absent.
- [x] **Step 3: Route resume and cancel through the per-session lock, reread checkpoint after lock acquisition, and persist pending cancellation.** Active execution returns `409 session_busy`.
- [x] **Step 4: Handle expiry at the API boundary and in the graph**; expired approval persists `expired`, returns HTTP 410, and never invokes the handler.
- [x] **Step 5: Confirm SSE generator cleanup releases its subscription/lock resources without interpreting `GeneratorExit` as explicit cancellation.**
- [x] **Step 6: Rerun API concurrency and compatibility tests; commit** as `feat: serialize session recovery and cancellation`.

## Task 5: Add Durable Event Cursor Replay

**Files:**
- Modify: `apps/api/src/erpilot_api/run_store.py`
- Modify: `apps/api/src/erpilot_api/service.py`
- Modify: `apps/api/src/erpilot_api/main.py`
- Test: `apps/api/tests/test_run_store.py`
- Test: `apps/api/tests/test_api.py`

**Interfaces:**
- Event append assigns each run a monotonically increasing `seq` and enforces unique `(run_id, seq)` in one transaction.
- `read_events(run_id: str, after_seq: int) -> list[dict]` returns ascending events with `seq > after_seq`.
- Add `GET /api/runs/{run_id}/events?after_seq=N`: emit persisted events after N, then follow in-process notifications; reconnect replays from durable storage.
- Snapshot includes the last persisted sequence for its run. Missing projection does not affect graph recovery.
- Keep the existing 30-day cleanup for terminal runs only; retain event rows for active, pending, approved-unresolved, and `unknown` runs. Before adding the unique sequence index, detect historical duplicates and fail without deleting or reordering rows.

- [x] **Step 1: Add failing RunStore tests** for unique ordering, cursor boundaries, duplicate legacy sequences, deduplication, and retention of unknown runs.
- [x] **Step 2: Add API tests** for invalid cursor, unknown run, replay boundaries, and snapshot watermarks.
- [x] **Step 3: Check for duplicate `(run_id, seq)` rows before creating the unique index.** Upgrade refuses collision and preserves both rows.
- [x] **Step 4: Add unique `(run_id, seq)` constraint and transactional sequence allocation.** Projection fields remain compatible.
- [x] **Step 5: Implement ascending replay and polling follow after cursor**; polling avoids a handoff gap and requires no in-process callback from sync writers.
- [x] **Step 6: Keep 30-day pruning limited to terminal runs** and test that unknown rows survive.
- [x] **Step 7: Add the route and snapshot watermark**; test replay and empty-current-cursor responses.
- [x] **Step 8: Run API/RunStore suites and Ruff; commit** as `feat: replay run events from durable cursor`.

## Task 6: Hydrate the Web Session from Snapshot and Cursor

**Files:**
- Modify: `apps/web/src/protocol.ts`
- Modify: `apps/web/src/session.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/components/WorkTimeline.tsx`
- Test: `apps/web/tests/protocol.test.mjs`
- Test: `apps/web/tests/session.test.mjs`

**Interfaces:**
- Session snapshot types represent `pending`, `approved`, `denied`, `expired`, `cancelled`, `succeeded`, `failed`, and `unknown` outcomes without treating `unknown` as failure or success.
- Event application de-duplicates by `(run_id, seq)` and associates tool events using both `call_id` and `pending_id`.
- Snapshot hydration replaces messages and answer text; event replay appends only events after `last_seq`.

- [x] **Step 1: Add protocol/session tests** for unknown state, event sequence de-duplication, cursor parsing and cancellation response.
- [x] **Step 2: Run `npm test` in `apps/web`** and correct type/state regressions before completing the change.
- [x] **Step 3: Add the typed snapshot, cancellation and event-stream contracts** while retaining legacy payload parsing.
- [x] **Step 4: Hydrate snapshot first and subscribe after `last_seq`; deduplicate by `(run_id, seq)` and render “结果待核对” and expired approval states.**
- [x] **Step 5: Rerun `npm test` and `npm run build` in `apps/web`; commit** as `feat: restore run timeline from event cursor`.

## Task 7: Prove R01–R10 with Hard Process Crashes

**Files:**
- Create: `apps/api/tests/fault_worker.py`
- Create: `apps/api/tests/test_process_recovery.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_recovery.py`
- Test fixtures: temporary PostgreSQL schema and temporary ERP SQLite database

**Interfaces:**
- Worker receives only a scenario name, isolated database URLs/paths, and a synchronization point; it exits via process termination at the requested boundary.
- Parent process owns assertions and records scenario/revision, checkpoint identifiers, pending ID, token, tool/arguments, injected boundary, database snapshots, handler count and outcome.
- Test matrix parameterizes the four mutation tools for R03–R06 and references all R01–R10 cases.

- [x] **Step 1: Add a child-worker smoke test** proving the parent observes a real process exit between a configured checkpoint and continuation.
- [x] **Step 2: Add isolated PostgreSQL schema and temporary ERP DB fixtures**; ensure teardown runs even when the child exits abruptly.
- [x] **Step 3: Implement deterministic crash hooks** before approval display, after approval checkpoint, before mutation commit, after commit/before graph result checkpoint, and before final answer.
- [x] **Step 4: Implement R01–R06 process tests**; parameterize R03–R06 for all four write tools. R07–R10 map to the API, graph, RunStore, and web deterministic suites listed in `test_process_recovery.py`.
- [x] **Step 5: Reuse negative safety cases** for expired approvals, lookup outage, same-token different-arguments conflict, unsupported tool version, and resume/cancel contention from the focused graph/API suites.
- [x] **Step 6: Run `uv run pytest apps/api/tests/test_process_recovery.py apps/api/tests/test_api.py apps/api/tests/test_run_store.py -q`** against isolated PostgreSQL and ERP databases; save each case result and trace path. 2026-10-08：46 passed，0 skipped。进程矩阵 JUnit 为 19/19，见 `reports/acceptance/2026-10-08-process-recovery.xml`。
- [x] **Step 7: Confirm all three zero-tolerance invariants** from database snapshots and invocation counters. 父进程断言：未批准/过期零写入、每个场景恰好一行 mutation、R04 只重试一次。提交为 `eaf2830`（`test: make crash recovery matrix runnable on Windows`）。

## Task 8: Final Integration and Acceptance Evidence

**Files:**
- Modify: `docs/adr/0010-crash-recovery-reconciliation-scope.md`
- Modify: `docs/superpowers/specs/2026-10-08-crash-recovery-reconciliation-design.md`
- Modify: `docs/superpowers/plans/2026-10-08-crash-recovery-reconciliation.md`
- Modify: `README.md`
- Create: `reports/acceptance/<revision>-crash-recovery.json`
- Create: `reports/acceptance/<revision>-crash-recovery.md`

**Interfaces:**
- Reports include revision/source hash, test and scenario versions, R01–R10 result per case, four-tool mapping, temporary DB before/after snapshots, handler count, IDs/token, injected crash boundary and failure diagnosis.
- ADR status becomes `已接受并实施` only after every acceptance gate passes and reviewers approve; otherwise retain proposal status and record exact failures.

- [x] **Step 1: Run `uv run ruff check .` and `uv run pytest`**; record command, revision and result. Ruff 通过。全量 pytest 有 2 个失败：展示保留期用例已改为只清理已结束 run 并复测通过；未提交的评测断言仍期望 scorer `v2.1`，当前脏工作区常量是 `v2.4`。
- [x] **Step 2: Run isolated PostgreSQL integration, `npm test`, and `npm run build` in `apps/web`.** 隔离库 `erpilot_test` 上的进程矩阵通过；web 49/49，生产构建通过。未使用开发 ERP 库。
- [x] **Step 3: Review every R01–R10 record and verify all four write tools have R03–R06 evidence.** R03–R06 四种写工具均在同一次 19/19 进程矩阵中通过。R07–R10 按计划对应 API、RunStore、graph 与 web 用例，这些用例在同日全量 pytest 中通过。不把多次运行拼成一次虚构的全绿。
- [x] **Step 4: Update README recovery/runtime notes and mark ADR accepted only if all gates pass.** 全量 pytest 仍有无关评测失败，且尚无评审接受，ADR-0010 保持提案。
- [ ] **Step 5: Run `git diff --check`, inspect status and final diff for unrelated changes or secrets; commit** as `docs: record crash recovery acceptance` only when evidence is complete.

## Dependency Order

```text
Task 1 ─┐
Task 2 ─┴→ Task 3 → Task 4 ─┬→ Task 5 → Task 6 ─┐
                            └───────────────────┴→ Task 7 → Task 8
```

## Self-Review

- Spec sections 1–2: Global Constraints and Task 8 preserve the single-process boundary, source-of-truth split, approval gate, immutable token and fail-closed behavior.
- Spec section 3: Tasks 1–3 implement graph metadata, mutation lookup and generic API adapter; Task 5 keeps RunStore display-only while adding replay.
- Spec section 4: Tasks 1, 3, and 4 implement expiration, reconciliation, resume/cancel serialization and no-compensation semantics.
- Spec section 5: Tasks 4–6 implement additive API contracts, snapshot watermark, durable replay and client de-duplication.
- Spec section 6: Task 7 exercises each R01–R10 and parameterizes R03–R06 across all four writes.
- Spec section 7: Task 8 requires full deterministic and frontend integration gates, with zero-tolerance checks.
- Spec section 8: Task 7 explicitly does not treat existing restart/approval demonstrations as complete R01–R10 evidence.
- The plan has no unassigned spec requirements; each test command targets repository paths present at plan creation.
