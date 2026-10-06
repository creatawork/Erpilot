# M6–8 LangGraph + PostgreSQL HITL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace the hand-written agent loop with a LangGraph runtime that preserves Erpilot's tool, event, and approval behavior while persisting graph checkpoints in PostgreSQL.

**Architecture:** `agent_core` owns the graph state, nodes, and event adapter while continuing to use the existing `LLMClient` and `Tool` interfaces. The API composition layer owns `AsyncPostgresSaver`; ERP mutations and the API `RunStore` remain on SQLite. Human approval uses LangGraph `interrupt`, stable tool tokens, and additive state/resume endpoints.

**Tech Stack:** Python 3.12, LangGraph, existing Pydantic/OpenAI-compatible client, FastAPI/SSE, PostgreSQL with `langgraph-checkpoint-postgres`, React + TypeScript.

**Spec:** [M6–8 design](../specs/2026-10-06-m6-8-langgraph-postgres-hitl-design.md)

## Global Constraints

- `agent_core` must not depend on ERP or MCP packages.
- ERP business data and API `RunStore` stay in SQLite; PostgreSQL stores LangGraph graph state only.
- The API supports one execution process; one session has at most one active graph invocation.
- API production mode uses `AsyncPostgresSaver`; missing/broken PostgreSQL configuration fails startup and never silently falls back to memory.
- `InMemorySaver` is only injected by tests or explicit no-database demos.
- Risk-bearing tools are unavailable unless the code-level approval gate is configured.
- Stable `client_token` and immutable normalized tool arguments are checkpointed before any write handler runs.
- Graph state contains serializable values only; never persist handlers, Futures, closures, or DB connections.
- Enable strict LangGraph MsgPack deserialization; do not automatically delete checkpoints.
- Preserve existing AgentEvent/SSE names and payloads. Add state/resume API paths without changing the legacy `{ok: boolean}` approval response.
- Do not run real-model evals or incur external model charges unless the user separately authorizes a budget.
- Do not implement the skipped recovery plan's T09–T12 / R01–R10 acceptance work.

## Review Focus

1. **Risk-bearing tool configured without approval:** graph construction must fail before any tool can run. Pin this in Task 5's `test_write_tools_require_approval_runtime`.
2. **Crash after business commit but before graph result checkpoint:** the retry must reuse the original token and return the original result. Pin this in Task 5's `test_write_retry_reuses_checkpointed_token`.
3. **Long or unusual session ID:** stable thread-ID derivation must stay within the saver limit and be deterministic. Pin this in Task 7's `test_thread_id_is_stable_and_bounded`.
4. **Approval from a stale page or for a different call:** unknown/stale pending IDs return a clear 404/409 and never execute a tool. Pin this in Task 10's `test_approval_resume_rejects_unknown_pending`.
5. **PostgreSQL unavailable at API startup:** startup fails with a clear configuration error and does not select `InMemorySaver`. Pin this in Task 8's `test_api_checkpoint_failure_is_fail_closed`.

---

## File Map

| File | Responsibility |
|---|---|
| `packages/agent_core/src/agent_core/graph_state.py` | Serializable LangGraph state and state-only helpers |
| `packages/agent_core/src/agent_core/graph.py` | Graph nodes, routes, step/context policy, and compilation |
| `packages/agent_core/src/agent_core/graph_tools.py` | Tool-call normalization, stable write tokens, validation, execution |
| `packages/agent_core/src/agent_core/graph_runtime.py` | Public runtime API, checkpoint config, stream-to-AgentEvent adapter |
| `packages/agent_core/src/agent_core/events.py` | Stable AgentEvent dataclasses shared by graph, traces, CLI and API |
| `packages/agent_core/src/agent_core/loop.py` | Legacy implementation until final cutover; then removed or reduced to compatibility re-exports |
| `packages/agent_core/pyproject.toml` | LangGraph runtime dependency |
| `packages/agent_core/tests/test_graph*.py` | Focused graph, approval, persistence-independent behavior tests |
| `apps/api/src/erpilot_api/checkpoint.py` | API-owned PostgreSQL saver and bounded thread-ID derivation |
| `apps/api/src/erpilot_api/service.py` | Session orchestration, SQLite projection, live approval broker and graph invocations |
| `apps/api/src/erpilot_api/main.py` | Lifespan, REST contracts, SSE endpoints and explicit runtime wiring |
| `apps/api/tests/test_checkpoint.py` | Thread-ID and PostgreSQL saver integration behavior |
| `apps/api/tests/test_api.py` | HTTP, SSE, state snapshot, approval resume and fail-closed behavior |
| `apps/web/src/protocol.ts` | Typed SSE/REST client contracts |
| `apps/web/src/App.tsx` | Snapshot hydration and connected/disconnected approval routing |
| `apps/web/tests/protocol.test.mjs` | API payload and SSE parsing contract tests |
| `apps/cli/src/erpilot_cli/main.py` | CLI construction and event handling for the graph runtime |
| `packages/evals/src/evals/runner.py` | Deterministic eval execution against explicit in-memory graph runtime |
| `packages/agent_core/src/agent_core/trace.py` | Trace recorder accepts the runtime/event interface rather than AgentLoop |
| `.github/workflows/ci.yml`, `compose.yaml`, `.env.example` | PostgreSQL dev/CI setup and safe runtime configuration |
| `README.md`, `docs/adr/0009-langgraph-runtime.md`, `docs/tool-cards.md` | User setup, accepted migration decision, tool contract sync |

## Task 1: Define serializable graph state

**Files:**
- Create: `packages/agent_core/src/agent_core/graph_state.py`
- Create: `packages/agent_core/tests/test_graph_state.py`

**Interfaces:**
- Produces `AgentState`, a `TypedDict` with `messages`, `step`, `usage`, `pending_calls`, `tool_results`, `completed`, and `final_answer` fields.
- Tool calls and results are `dict[str, object]`-shaped JSON data, not LangChain message objects or runtime instances.

- [x] **Step 1: Write state serialization and append/replace tests**

```python
def test_agent_state_contains_only_serializable_checkpoint_values():
    state = initial_state([{"role": "user", "content": "查库存"}])
    json.dumps(state, ensure_ascii=False)
    assert state["step"] == 0
    assert state["pending_calls"] == []
```

- [x] **Step 2: Run the focused test and confirm the missing state API fails**

Run: `uv run pytest packages/agent_core/tests/test_graph_state.py -q`
Expected: FAIL because `graph_state.py` and `initial_state` do not exist.

- [x] **Step 3: Add `AgentState` and `initial_state(messages)`**

Use plain dict/list/string/number/bool/null values. Keep `messages` replaceable so context compression can return a bounded history without an append-only reducer.

- [x] **Step 4: Run the focused test**

Run: `uv run pytest packages/agent_core/tests/test_graph_state.py -q`
Expected: PASS; JSON serialization raises no exception.

- [x] **Step 5: Commit the state contract**

```bash
git add packages/agent_core/src/agent_core/graph_state.py packages/agent_core/tests/test_graph_state.py
git commit -m "feat: define serializable graph state"
```

## Task 2: Build the model node with the existing LLM client

**Files:**
- Create: `packages/agent_core/src/agent_core/graph.py`
- Create: `packages/agent_core/tests/test_graph_model.py`
- Modify: `packages/agent_core/pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces `build_graph(client, tools, config, checkpointer) -> CompiledStateGraph`.
- The model node consumes `AgentState` and uses `LLMClient.stream_chat(messages, tools=...)`; it must not create a LangChain provider client.

- [x] **Step 1: Add a stub-client test for streamed text and usage**

Test one `TextDelta("答复")` and one `StreamEnd(usage=...)`; assert state contains the assistant answer and accumulated usage.

- [x] **Step 2: Run the focused test to confirm graph API is absent**

Run: `uv run pytest packages/agent_core/tests/test_graph_model.py -q`
Expected: FAIL because `build_graph` does not exist.

- [x] **Step 3: Add `langgraph` to agent-core and update the lockfile**

Run: `uv add --package agent-core langgraph`
Expected: `packages/agent_core/pyproject.toml` records the dependency and `uv.lock` resolves the workspace.

- [x] **Step 4: Implement the model node and minimal graph**

Stream current LLM events, gather text/tool calls, append one OpenAI-compatible assistant message, increment `step`, and route to a terminal state when there are no tool calls.

- [x] **Step 5: Run the focused model-node tests**

Run: `uv run pytest packages/agent_core/tests/test_graph_model.py -q`
Expected: PASS for final text, assistant message format, usage aggregation, and checkpointer injection.

- [x] **Step 6: Commit the minimal model graph**

```bash
git add packages/agent_core/pyproject.toml uv.lock packages/agent_core/src/agent_core/graph.py packages/agent_core/tests/test_graph_model.py
git commit -m "feat: add LangGraph model node"
```

## Task 3: Preserve loop limits and context policy

**Files:**
- Modify: `packages/agent_core/src/agent_core/graph.py`
- Modify: `packages/agent_core/src/agent_core/graph_state.py`
- Create: `packages/agent_core/tests/test_graph_control_flow.py`

**Interfaces:**
- Consumes existing `LoopConfig`, `ContextPolicy`, and `compress_messages`.
- Produces max-step termination with the existing `LoopEnd(completed=False)` behavior and one-based step numbering.

- [x] **Step 1: Add tests for step numbering, max steps, and context compression**
- [x] **Step 2: Run `uv run pytest packages/agent_core/tests/test_graph_control_flow.py -q` and confirm the expected failures**
- [x] **Step 3: Apply `compress_messages` before each model request and route by `step < max_steps`**

```python
if state["step"] >= config.max_steps:
    return {"completed": False}
compress_messages(state["messages"], config.context)
```

- [x] **Step 4: Re-run the focused control-flow tests and confirm PASS**
- [x] **Step 5: Commit the control-flow slice**

```bash
git add packages/agent_core/src/agent_core/graph.py packages/agent_core/src/agent_core/graph_state.py packages/agent_core/tests/test_graph_control_flow.py
git commit -m "feat: preserve agent step and context policies"
```

## Task 4: Normalize calls and execute read tools

**Files:**
- Create: `packages/agent_core/src/agent_core/graph_tools.py`
- Modify: `packages/agent_core/src/agent_core/graph.py`
- Create: `packages/agent_core/tests/test_graph_tools.py`

**Interfaces:**
- Produces `prepare_tool_calls(calls, tools) -> list[dict]` and async `execute_tool_call(call, tools, config) -> dict`.
- Each call record includes `call_id`, `name`, normalized `arguments`, `risk`, `client_token`, `pending_id`, and result fields when complete.

- [x] **Step 1: Add tests for Pydantic argument validation, unknown tools, and call/result ID pairing**
- [x] **Step 2: Run the focused tests and confirm missing helpers fail**

Run: `uv run pytest packages/agent_core/tests/test_graph_tools.py -q`

- [x] **Step 3: Implement normalization and JSON-shaped structured errors**

Malformed arguments return the existing `validation` error envelope; unregistered names return `unknown_tool`. Neither path invokes a handler.

- [x] **Step 4: Add parallel read-only fan-out and retry-safe transient handling**

Use `asyncio.gather` for calls with `risk is None`; preserve result-to-`call_id` pairing even when completion order differs. Reuse existing `ToolRetryPolicy` semantics.

- [x] **Step 5: Run the focused tool tests**

Run: `uv run pytest packages/agent_core/tests/test_graph_tools.py -q`
Expected: PASS for valid calls, error envelopes, timeout retry policy, and parallel result pairing.

- [x] **Step 6: Commit read-tool execution**

```bash
git add packages/agent_core/src/agent_core/graph_tools.py packages/agent_core/src/agent_core/graph.py packages/agent_core/tests/test_graph_tools.py
git commit -m "feat: execute graph read tools"
```

## Task 5: Add approval interrupts and stable write tokens

**Files:**
- Create: `packages/agent_core/src/agent_core/graph_approval.py`
- Modify: `packages/agent_core/src/agent_core/graph_tools.py`
- Modify: `packages/agent_core/src/agent_core/graph.py`
- Create: `packages/agent_core/tests/test_graph_approval.py`

**Interfaces:**
- Produces `make_approval_payload(call) -> dict` and a write-task node that invokes `interrupt(payload)` before calling any write handler.
- Resuming supplies only `{"approved": bool, "reason": str, "pending_id": str}`; tool name and normalized arguments come from checkpointed state.

- [x] **Step 1: Add tests for missing gate, pending payload, reject path, and approve path**
- [x] **Step 2: Run `uv run pytest packages/agent_core/tests/test_graph_approval.py -q` and confirm failures**
- [x] **Step 3: Assign UUID `client_token` and `pending_id` in the call preparation node**

```python
if call["risk"] is not None:
    call["arguments"]["client_token"] = call["arguments"].get("client_token") or uuid4().hex
    call["pending_id"] = uuid4().hex
```

- [x] **Step 4: Implement one interrupt per graph task and execute only after an approved resume**

The approval node reads call data from state, calls `interrupt()` before side effects, validates the returned pending ID, and then either returns the structured denial result or invokes the original handler using the same checkpointed arguments.

- [x] **Step 5: Verify tool construction rejects writes without approval runtime**

Run: `uv run pytest packages/agent_core/tests/test_graph_approval.py::test_write_tools_require_approval_runtime -q`
Expected: PASS; the handler call count remains zero.

- [x] **Step 6: Verify replay reuses token and same-token/different-arguments conflict is surfaced**

Run: `uv run pytest packages/agent_core/tests/test_graph_approval.py -q`
Expected: PASS; approved retry uses the identical token and never changes the normalized request.

- [x] **Step 7: Commit the approval node**

```bash
git add packages/agent_core/src/agent_core/graph_approval.py packages/agent_core/src/agent_core/graph_tools.py packages/agent_core/src/agent_core/graph.py packages/agent_core/tests/test_graph_approval.py
git commit -m "feat: gate graph writes with interrupts"
```

## Task 6: Expose runtime and preserve AgentEvent streaming

**Files:**
- Create: `packages/agent_core/src/agent_core/events.py`
- Create: `packages/agent_core/src/agent_core/graph_runtime.py`
- Modify: `packages/agent_core/src/agent_core/loop.py`
- Create: `packages/agent_core/tests/test_graph_runtime.py`

**Interfaces:**
- Produces `LangGraphRuntime(client, tools, config, checkpointer)` with async `stream(messages, *, thread_id)`, `get_state(thread_id)`, and `resume(thread_id, decision)` methods.
- `stream` yields existing `AgentEvent` dataclasses. `events.py` owns the dataclasses; `loop.py` re-exports them during migration.

- [x] **Step 1: Add a compatibility test comparing event names and payload fields**
- [x] **Step 2: Run `uv run pytest packages/agent_core/tests/test_graph_runtime.py -q` and confirm failure**
- [x] **Step 3: Move event dataclass definitions to `events.py` and re-export from `loop.py`**
- [x] **Step 4: Adapt graph stream/custom/interrupt events to `TextDelta`, step, tool, approval, and loop events**

The adapter must emit each tool event with the originating `call_id`; interrupt payloads map back to `ApprovalPending` and decisions map to `ApprovalResolved`.

- [x] **Step 5: Implement `stream`, `get_state`, and `resume` using one stable `thread_id`**
- [x] **Step 6: Run runtime and existing protocol tests**

Run: `uv run pytest packages/agent_core/tests/test_graph_runtime.py packages/agent_core/tests/test_loop.py -q`
Expected: PASS; existing loop tests remain valid while the graph runtime emits compatible events.

- [x] **Step 7: Commit the public runtime interface**

```bash
git add packages/agent_core/src/agent_core/events.py packages/agent_core/src/agent_core/graph_runtime.py packages/agent_core/src/agent_core/loop.py packages/agent_core/tests/test_graph_runtime.py
git commit -m "feat: expose LangGraph runtime events"
```

### M6 checkpoint

- [x] Graph runtime passes focused stub-client, tool, approval, and event contract tests using explicit `InMemorySaver`.
- [x] `uv run ruff check packages/agent_core` passes.
- [x] Do not switch API/CLI/eval consumers yet.

## Task 7: Add API-owned PostgreSQL checkpointer configuration

**Files:**
- Create: `apps/api/src/erpilot_api/checkpoint.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `uv.lock`
- Modify: `.env.example`
- Create: `apps/api/tests/test_checkpoint.py`

**Interfaces:**
- Produces `thread_id_for_session(session_id: str) -> str` using a namespaced SHA-256 digest no longer than the saver limit.
- Produces async context manager `open_checkpointer(database_url) -> AsyncIterator[AsyncPostgresSaver]`; `setup()` completes before it is returned.

- [x] **Step 1: Add tests for stable/bounded thread IDs and missing database URL**
- [x] **Step 2: Run `uv run pytest apps/api/tests/test_checkpoint.py -q` and confirm failure**
- [x] **Step 3: Add `langgraph-checkpoint-postgres` to API dependencies and update `uv.lock`**

Run: `uv add --package erpilot-api langgraph-checkpoint-postgres`

- [x] **Step 4: Implement saver construction, `.setup()`, strict serializer configuration, and connection cleanup**
- [x] **Step 5: Add `ERPILOT_CHECKPOINT_DATABASE_URL` to `.env.example` without a real credential**
- [x] **Step 6: Run focused config tests and commit**

Run: `uv run pytest apps/api/tests/test_checkpoint.py -q`

```bash
git add apps/api/src/erpilot_api/checkpoint.py apps/api/pyproject.toml apps/api/tests/test_checkpoint.py .env.example uv.lock
git commit -m "feat: configure PostgreSQL graph checkpoints"
```

## Task 8: Wire API lifespan and PostgreSQL integration coverage

**Files:**
- Modify: `apps/api/src/erpilot_api/main.py`
- Modify: `apps/api/tests/test_api.py`
- Modify: `.github/workflows/ci.yml`
- Create: `compose.yaml`
- Modify: `README.md`

**Interfaces:**
- `create_app` accepts an injected saver for tests; production lifespan opens the PostgreSQL saver and closes it after requests finish.
- PostgreSQL is an explicit API requirement; test/demo in-memory saver injection is never selected by production fallback.

- [x] **Step 1: Add API-factory tests for injected saver and startup failure**
- [x] **Step 2: Run the focused API tests and confirm the production lifecycle is not wired**
- [x] **Step 3: Add FastAPI lifespan ownership and pass saver to `LangGraphRuntime` factory**
- [x] **Step 4: Add a local `postgres` service in `compose.yaml` with healthcheck and a non-production database**
- [x] **Step 5: Add PostgreSQL service to CI and run integration tests only after it is healthy**
- [x] **Step 6: Document `docker compose up -d postgres` and required API environment configuration**
- [x] **Step 7: Verify failure is fail-closed and commit**

Run: `docker compose up -d postgres` then `uv run pytest apps/api/tests/test_api.py -q`
Expected: PASS with PostgreSQL; a missing/unavailable URL fails application lifespan and never selects memory storage.

```bash
git add apps/api/src/erpilot_api/main.py apps/api/tests/test_api.py .github/workflows/ci.yml compose.yaml README.md
git commit -m "feat: wire PostgreSQL saver into API lifespan"
```

## Task 9: Prove checkpoint recovery across runtime instances

**Files:**
- Modify: `apps/api/tests/test_checkpoint.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `compose.yaml`

**Interfaces:**
- Consumes `open_checkpointer` and `LangGraphRuntime` from earlier tasks.
- Produces a deterministic integration proof that a pending interrupt can be resumed by a newly constructed runtime using the same PostgreSQL thread.

- [x] **Step 1: Add a test that interrupts an approval graph and closes the first saver/runtime**
- [x] **Step 2: Reopen the saver/runtime and assert the pending payload and call arguments are unchanged**
- [x] **Step 3: Resume with a denial and assert zero handler calls**
- [x] **Step 4: Resume a separate approved run and assert the same `client_token` reaches the handler**
- [x] **Step 5: Run `uv run pytest apps/api/tests/test_checkpoint.py -q` against the isolated CI/local PostgreSQL service**
- [x] **Step 6: Commit the cross-instance proof**

```bash
git add apps/api/tests/test_checkpoint.py .github/workflows/ci.yml compose.yaml
git commit -m "test: prove Postgres approval checkpoint recovery"
```

## Task 10: Move ChatService and REST approval flows to graph runtime

**Files:**
- Modify: `apps/api/src/erpilot_api/service.py`
- Modify: `apps/api/src/erpilot_api/main.py`
- Modify: `apps/api/src/erpilot_api/run_store.py`
- Modify: `apps/api/tests/test_api.py`
- Modify: `apps/api/tests/test_run_store.py`

**Interfaces:**
- `ChatService` owns `LangGraphRuntime`, per-session serialization, and a live approval broker for existing SSE connections.
- New `GET /api/sessions/{session_id}/state` returns `{session_id, status, messages, pending_approvals}`.
- New `POST /api/chat/approve/stream` accepts `session_id`, `pending_id`, `approved`, and optional `reason`, then streams resumed AgentEvents.
- Legacy `POST /api/chat/approve` keeps `{ok: boolean}` for an active in-process waiter.

- [x] **Step 1: Add service tests for graph-owned messages and completed-history seeding from SQLite RunStore**
- [x] **Step 2: Add route tests for snapshot shape, 404 unknown session, 409 stale/repeated approval, and legacy `{ok}` semantics**
- [x] **Step 3: Run focused API tests and confirm the graph endpoints are absent**
- [x] **Step 4: Switch `ChatService` to graph stream/resume while keeping SQLite only as the completed-history/display projection**

When a checkpoint exists, load its messages. Only when none exists, seed from completed SQLite session history; never reconstruct or replay an incomplete legacy run.

- [x] **Step 5: Implement live broker continuation and state/approval-stream routes**
- [x] **Step 6: Verify event errors and REST errors use the shapes in the spec**

Run: `uv run pytest apps/api/tests/test_api.py apps/api/tests/test_run_store.py -q`
Expected: PASS for active approval continuation, post-disconnect resume, old `{ok}` response, and unchanged RunStore SQLite behavior.

- [x] **Step 7: Commit API graph orchestration**

```bash
git add apps/api/src/erpilot_api/service.py apps/api/src/erpilot_api/main.py apps/api/src/erpilot_api/run_store.py apps/api/tests/test_api.py apps/api/tests/test_run_store.py
git commit -m "feat: resume API approval through LangGraph"
```

## Task 11: Add frontend session hydration and resumed approval stream

**Files:**
- Modify: `apps/web/src/protocol.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/tests/protocol.test.mjs`

**Interfaces:**
- `loadSessionState(sessionId)` parses the session snapshot.
- `streamApprovalResume(sessionId, pendingId, approved, reason?)` consumes the same SSE event union as `streamChat`.
- Existing active SSE approval uses `submitApproval`; disconnected/stale UI uses the resume stream.

- [x] **Step 1: Add protocol tests for state snapshot JSON, approval stream fields, event parsing, and server error events**
- [x] **Step 2: Run `npm test` in `apps/web` and confirm missing client functions fail type checking/tests**
- [x] **Step 3: Implement typed snapshot and approval-resume fetch helpers**
- [x] **Step 4: Hydrate the conversation and pending cards from snapshot when the app loads**
- [x] **Step 5: Route approval through the connected endpoint only while the chat SSE remains active; otherwise consume the resume SSE**
- [x] **Step 6: Run `npm test` and `npm run build` in `apps/web`**
- [x] **Step 7: Commit frontend resume support**

```bash
git add apps/web/src/protocol.ts apps/web/src/App.tsx apps/web/tests/protocol.test.mjs
git commit -m "feat: resume graph approvals in the web client"
```

## Task 12: Switch CLI and trace recorder to LangGraph runtime

**Files:**
- Modify: `apps/cli/src/erpilot_cli/main.py`
- Modify: `packages/agent_core/src/agent_core/trace.py`
- Modify: `apps/cli/tests/test_cli.py`
- Modify: `packages/agent_core/tests/test_trace.py`

**Interfaces:**
- CLI builds graph runtime with an explicit saver and a unique thread ID per new conversation.
- Trace recorder consumes the shared event/runtime protocol, not the concrete `AgentLoop` class.

- [x] **Step 1: Add CLI and trace tests that inject `InMemorySaver` and a deterministic LLM stub**
- [x] **Step 2: Run focused tests and confirm current AgentLoop constructors are the only path**
- [x] **Step 3: Update CLI construction and approval decision resume to use graph runtime**
- [x] **Step 4: Update trace recorder to pass a stable run thread and serialize shared AgentEvents**
- [x] **Step 5: Run `uv run pytest apps/cli/tests/test_cli.py packages/agent_core/tests/test_trace.py -q`**
- [x] **Step 6: Commit CLI/trace cutover**

```bash
git add apps/cli/src/erpilot_cli/main.py apps/cli/tests/test_cli.py packages/agent_core/src/agent_core/trace.py packages/agent_core/tests/test_trace.py
git commit -m "feat: use LangGraph runtime in CLI and traces"
```

## Task 13: Switch evaluation and demo consumers

**Files:**
- Modify: `packages/evals/src/evals/runner.py`
- Modify: `packages/evals/tests/test_runner.py`
- Modify: `packages/agent_core/src/agent_core/__main__.py`
- Modify: `packages/agent_core/tests/test_smoke.py`

**Interfaces:**
- Eval runner creates one isolated `InMemorySaver` per case and a deterministic thread ID scoped to that case.
- Demo runner uses explicit memory state and does not call a real model unless the existing command is explicitly configured to do so.

- [x] **Step 1: Add runner tests proving case state cannot leak between cases**
- [x] **Step 2: Update eval and demo runtime construction while preserving existing case-level tool gates**
- [x] **Step 3: Run `uv run pytest packages/evals/tests/test_runner.py packages/agent_core/tests/test_smoke.py -q`**
- [x] **Step 4: Run a no-network scripted demo and inspect its trace/event order**
- [x] **Step 5: Commit eval/demo cutover**

```bash
git add packages/evals/src/evals/runner.py packages/evals/tests/test_runner.py packages/agent_core/src/agent_core/__main__.py packages/agent_core/tests/test_smoke.py
git commit -m "feat: use LangGraph runtime in evals and demos"
```

## Task 14: Remove the production loop and update architecture docs

**Files:**
- Modify: `packages/agent_core/src/agent_core/loop.py`
- Modify: `packages/agent_core/src/agent_core/__init__.py`
- Modify: `packages/mcp_erp/tests/test_mcp_erp.py`
- Modify: `docs/adr/0009-langgraph-runtime.md`
- Modify: `docs/adr/0008-persistent-run-recovery.md`
- Modify: `docs/tool-cards.md`
- Modify: `README.md`

**Interfaces:**
- No production consumer imports `AgentLoop` after this task.
- AgentEvent types remain importable from `agent_core.events`; compatibility re-exports stay only if a documented consumer needs them.

- [x] **Step 1: Search all tracked source/tests for `AgentLoop` imports**

Run: `git grep -n AgentLoop -- apps packages`
Expected before cleanup: only legacy implementation/tests and the consumers explicitly listed in earlier tasks.

- [x] **Step 2: Migrate MCP bridge integration tests to `LangGraphRuntime`**
- [x] **Step 3: Remove the old loop implementation and update package exports**
- [x] **Step 4: Add ADR-0009 as accepted only after implementation is complete; update ADR-0008 wording to preserve its historical rationale and record the approved later migration**
- [x] **Step 5: Update README commands/configuration and tool-card sync text**
- [x] **Step 6: Run `git grep -n AgentLoop -- apps packages`; confirm no production imports remain**
- [x] **Step 7: Commit cleanup and docs**

```bash
git add packages/agent_core/src/agent_core/loop.py packages/agent_core/src/agent_core/__init__.py packages/mcp_erp/tests/test_mcp_erp.py docs/adr/0009-langgraph-runtime.md docs/adr/0008-persistent-run-recovery.md docs/tool-cards.md README.md
git commit -m "refactor: retire handwritten agent loop"
```

## Task 15: Run the offline migration gates

**Files:**
- No planned code changes; fix only failures within the owning earlier task.

- [x] **Step 1: Run `uv run ruff check .`**
- [x] **Step 2: Run `uv run pytest`**
- [x] **Step 3: Run PostgreSQL saver/API integration tests with isolated PostgreSQL**
- [x] **Step 4: Run `npm test` and `npm run build` in `apps/web`**
- [x] **Step 5: Run the scripted no-network demo and review the approval/denial event sequence**
- [x] **Step 6: Inspect `git diff --check`, `git status`, and the final diff for secrets or unintended ERP schema changes**
- [x] **Step 7: Summarize any unrun real-model evaluation as unverified; do not claim the skipped recovery acceptance passed**

## Dependency Order

```text
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6
                                                   ├→ Task 7 → Task 8 → Task 9 → Task 10 → Task 11 ─┐
                                                   │                                      └→ Task 12 ├→ Task 14 → Task 15
                                                   └→ Task 13 ────────────────────────────────────────┘
```

## Self-Review

- Spec sections 1–2: Tasks 7–15 retain the SQLite data boundary, skip legacy recovery acceptance, and exclude real-model spending.
- Spec section 3: Tasks 1–6 define graph state, graph/tool/approval nodes, runtime boundaries, and event compatibility.
- Spec section 4: Tasks 7–9 define thread IDs, API-owned saver lifecycle, strict serialization, and restart persistence.
- Spec section 5: Tasks 10–11 implement state snapshot, connected approval, resumed SSE, validation, and frontend contract.
- Spec section 6: Tasks 1–6 are M6, Tasks 7–10 are M7, Tasks 11–15 complete M8 and cutover.
- Spec section 7: Each acceptance criterion is exercised in Tasks 1–15, including fail-closed startup and stable token replay.
- Spec section 8: Review Focus entries map to test names in Tasks 5, 7, 8, and 10; storage split and old waiter cutover are handled in Tasks 9–10.
- No placeholders remain; interfaces used by later tasks are declared by earlier tasks or in the spec.
