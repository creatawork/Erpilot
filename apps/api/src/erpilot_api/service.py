"""会话管理 + agent 宿主：每个会话一份消息历史，每次 run 顺带落一份 trace。

工具集由组合层注入（评审遗留项，M3 第 2 周落地）——api 本体不关心工具来自
MCP 桥还是演示假数据。M1 第 4 周的最小实现：单进程内存会话（重启即失）、
trace 本地 JSONL。持久化（Postgres checkpointer）与远程可观测（Langfuse）
按路线图在 M6 前后接入，届时替换的是本模块的存储层，事件协议与前端不动。
"""

import asyncio
import copy
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from agent_core.approval import ApprovalDecision
from agent_core.demo_tools import system_prompt
from agent_core.llm import LLMClient
from agent_core.loop import (
    AgentEvent,
    AgentLoop,
    ApprovalPending,
    ApprovalResolved,
    LoopConfig,
    LoopEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from agent_core.tools import Tool
from agent_core.trace import JsonlTraceRecorder, TraceSink, new_trace_path

from erpilot_api.run_store import (
    TERMINAL_INVOCATION_STATUSES,
    ApprovalStateError,
    RunStore,
    classify_tool_result,
)

DEFAULT_APPROVAL_TTL_SECONDS = 1800  # 30 分钟（恢复协议明细 §4.2；env 可配）


def _approval_ttl() -> int:
    raw = os.environ.get("ERPILOT_APPROVAL_TTL_SECONDS", "")
    return int(raw) if raw.isdigit() else DEFAULT_APPROVAL_TTL_SECONDS


def _schema_sha(params_model: Any) -> str:
    """参数模型 JSON Schema 的 sha256：适配层未带 schema_version 时的兜底。"""
    canonical = json.dumps(params_model.model_json_schema(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sinks_from_env() -> list[TraceSink]:
    """Langfuse keys 齐全 → 双写远程 sink；否则本地 JSONL 独挑（ADR-0003）。"""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from agent_core.observability import LangfuseTraceSink

        return [LangfuseTraceSink()]
    except Exception as exc:  # SDK 未装 / 配置错：本地兜底仍在，服务不因此起不来
        print(f"[trace] Langfuse 双写未启用：{exc}")
        return []


class ChatService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], LLMClient],
        model: str,
        trace_dir: Path,
        tools: Sequence[Tool],
        loop_config: LoopConfig | None = None,
        approval_gate: Any | None = None,
        run_store: RunStore | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._model = model
        self._trace_dir = trace_dir
        self._tools = list(tools)
        self._loop_config = loop_config or LoopConfig()
        self._approval_gate = approval_gate
        self._run_store = run_store
        self._sinks = _sinks_from_env()
        self._approval_ttl = _approval_ttl()
        self._tool_schema_versions = {
            t.name: (t.schema_version or _schema_sha(t.params_model)) for t in tools
        }
        self._client: LLMClient | None = None
        self._sessions: dict[str, list] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_trace: Path | None = None  # 最近一次 run 的 trace 文件（done 事件带回前端）

    @property
    def model(self) -> str:
        return self._model

    def respond_approval(self, pending_id: str, decision: ApprovalDecision, **identity) -> bool:
        """回填一次审批决策；无门或 pending_id 无效返回 False（不抛异常）。"""
        if self._run_store and self._run_store.get_approval(pending_id):
            approval = self._run_store.get_approval(pending_id)
            inv = self._run_store.get_invocation(approval["invocation_id"])
            if decision.approved and approval["status"] == "pending" and (
                self._tool_schema_versions.get(inv["tool"]) != inv["tool_schema_version"]
            ):
                raise ApprovalStateError(409, "工具版本已变化，原批准不可迁移", approval)
            self._run_store.decide_approval(
                pending_id, decision.approved, decision.reason, **identity,
            )
            if self._approval_gate:
                self._approval_gate.respond(pending_id, decision)
            return True
        if self._approval_gate is None:
            return False
        return self._approval_gate.respond(pending_id, decision)

    def lock(self, session_id: str) -> asyncio.Lock:
        """每会话一把锁：同一会话的多次 run 串行，历史才不会交错。"""
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _session_messages(self, session_id: str) -> list:
        if session_id not in self._sessions:
            saved = self._run_store.get_session(session_id) if self._run_store else None
            self._sessions[session_id] = saved["history"] if saved else [
                {"role": "system", "content": system_prompt(any(t.risk for t in self._tools))}
            ]
        return self._sessions[session_id]

    def start_run(
        self, session_id: str, message: str
    ) -> tuple[AgentLoop, list, JsonlTraceRecorder]:
        """把用户消息追加进会话历史，建好本轮的 agent 与 trace recorder。"""
        if self._client is None:
            self._client = self._client_factory()
        messages = self._session_messages(session_id)
        messages.append({"role": "user", "content": message})
        agent = AgentLoop(self._client, tools=self._tools, config=self._loop_config)
        trace_path = new_trace_path(self._trace_dir, self._model)
        self.last_trace = trace_path
        recorder = JsonlTraceRecorder(trace_path, self._model, sinks=self._sinks)
        return agent, messages, recorder

    async def stream_run(
        self, session_id: str, message: str, *, existing_run_id: str | None = None,
        recovered_messages: list | None = None, answer_only: bool = False,
        preserve_interruption: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """带会话锁地跑一轮：调用方直接迭代事件，串行与历史维护都在这层。"""
        async with self.lock(session_id):
            previous = copy.deepcopy(self._session_messages(session_id))
            if existing_run_id is None:
                agent, messages, recorder = self.start_run(session_id, message)
            else:
                if self._client is None:
                    self._client = self._client_factory()
                messages = (
                    copy.deepcopy(recovered_messages) if recovered_messages is not None else
                    [*previous, {"role": "user", "content": message}]
                )
                self._sessions[session_id] = messages
                agent = AgentLoop(
                    self._client,
                    tools=[t for t in self._tools if not answer_only or t.risk is None],
                    config=self._loop_config,
                )
                run = self._run_store.get_run(existing_run_id)
                path = Path(run["trace_path"]) if run["trace_path"] else new_trace_path(
                    self._trace_dir, self._model,
                )
                recorder = JsonlTraceRecorder(path, self._model, sinks=self._sinks)
            stream = recorder.run(agent, messages)
            finished = False
            interrupted = False
            run_id: str | None = existing_run_id
            try:
                if self._run_store and run_id is None:
                    run_id = self._run_store.create_run(
                        session_id, message, previous, str(self.last_trace)
                    )
                async for event in stream:
                    if isinstance(event, ToolCallStarted) and run_id:
                        # 完整 assistant 调用组先落盘；已完成的逐调用结果由事件持久化补齐。
                        self._run_store.checkpoint_run(run_id, messages)
                    # T05（ADR-0008 W1）：审批展示前固化写调用身份——token/call_id/
                    # 规范化参数/pending_id 同事务落盘，恢复不再依赖闭包；
                    # 无稳定 token（调用方未接恢复层）不落。
                    if (
                        isinstance(event, ApprovalPending)
                        and run_id and self._run_store and event.client_token
                    ):
                        self._run_store.record_write_intent(
                            run_id,
                            event.call_id,
                            session_id=session_id,
                            tool=event.tool,
                            tool_schema_version=self._tool_schema_versions.get(event.tool, ""),
                            arguments=event.arguments,
                            client_token=event.client_token,
                            pending_id=event.pending_id,
                            ttl_seconds=self._approval_ttl,
                        )
                    if isinstance(event, ApprovalResolved) and run_id and self._run_store:
                        # 决策落盘（尽力而为）：进程内 gate 已裁决，这里补持久面；
                        # 竞争/已决由 run_store 条件更新拒绝，不影响进行中的执行。
                        self._run_store.record_approval_decision(
                            event.pending_id, event.approved, event.reason
                        )
                        if event.approved:
                            inv = self._run_store.get_invocation_by_call(run_id, event.call_id)
                            if inv:
                                self._run_store.set_invocation_status(
                                    inv["invocation_id"], "executing",
                                )
                    if isinstance(event, ToolCallFinished) and run_id and self._run_store:
                        # T06（W6）：工具结果落盘，回答重建不再重放写调用。
                        # ok=False 只会是超时/执行异常（业务错误走 ok=True 的
                        # 错误契约）——事务可能已提交（ADR-0007），标可对账的
                        # unknown 而非终态 failed，恢复时按 token 收口。
                        invocation = self._run_store.get_invocation_by_call(run_id, event.call_id)
                        if invocation and invocation["status"] not in TERMINAL_INVOCATION_STATUSES:
                            status, result = (
                                classify_tool_result(event.content)
                                if event.ok else ("unknown", None)
                            )
                            self._run_store.set_invocation_status(
                                invocation["invocation_id"], status, result,
                            )
                    if isinstance(event, StepStarted) and event.step > 1 and run_id:
                        # The previous tool group has finished and its messages are complete.
                        self._run_store.checkpoint_run(run_id, messages)
                    if isinstance(event, LoopEnd) and event.completed:
                        if run_id:
                            answer = str(messages[-1].get("content") or "")
                            if not self._run_store.finish_run(run_id, messages, answer):
                                # 模型回答结束不代表写结果确定；保留运行检查点待对账，
                                # 不把未核对的结果发布到会话历史，也不经 finally 标 failed。
                                messages[:] = previous
                        finished = True
                    yield event
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                await stream.aclose()
                if not finished:
                    if run_id:
                        if interrupted and preserve_interruption:
                            self._run_store.set_run_status(run_id, "recovering")
                        else:
                            self._run_store.fail_run(run_id, "run interrupted before completion")
                    # 不保留缺少 tool 回填的半轮历史，后续请求仍符合模型协议。
                    messages[:] = previous
