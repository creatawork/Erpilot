"""Single-process run ownership, durable events and checkpoint continuation."""

import asyncio
import contextlib
import copy
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from uuid import uuid4

from agent_core.approval import ApprovalDecision
from agent_core.loop import LoopEnd
from agent_core.prices import cost_of
from agent_core.trace import new_trace_path

from erpilot_api.events import encode_event
from erpilot_api.recovery import RecoveryCoordinator, RecoveryError
from erpilot_api.run_store import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_INVOCATION_STATUSES,
    RunStore,
    compute_fingerprint,
)
from erpilot_api.service import ChatService


def model_result(invocation: dict):
    result = invocation["result"]
    if not isinstance(result, dict) or "committed_result" not in result:
        return result
    committed = result["committed_result"]
    tool = invocation["tool"]
    if tool == "adjust_stock":
        return {"sku": committed[0], "quantity": committed[1], "recovered": True}
    if tool == "set_product_status":
        return {"sku": committed, "status": invocation["arguments"]["status"], "recovered": True}
    return committed


class RunManager:
    def __init__(self, service: ChatService, store: RunStore,
                 recovery: RecoveryCoordinator | None = None):
        self.service = service
        self.store = store
        self.recovery = recovery
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, session_id: str, message: str) -> str:
        history = copy.deepcopy(self.service._session_messages(session_id))
        trace = new_trace_path(self.service._trace_dir, self.service.model)
        run_id = self.store.create_run(session_id, message, history, str(trace))
        self._launch(run_id, resumed=False)
        return run_id

    async def resume(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] in ACTIVE_RUN_STATUSES:
            self.store.get_session(run["session_id"])
            if self.store.list_invocations(run_id) and self.recovery is None:
                raise RecoveryError("未配置业务 token 对账能力，禁止恢复写任务")
            for inv in self.store.list_invocations(run_id):
                if inv["status"] in TERMINAL_INVOCATION_STATUSES:
                    continue
                if (
                    self.service._tool_schema_versions.get(inv["tool"])
                    != inv["tool_schema_version"]
                ):
                    raise RecoveryError("工具版本不兼容，原批准不可继承")
                fingerprint = compute_fingerprint(
                    inv["tool"], inv["tool_schema_version"], inv["arguments"],
                )
                if fingerprint != inv["arguments_fingerprint"]:
                    raise RecoveryError("调用参数指纹不兼容")
            self._launch(run_id, resumed=True)
        return self.store.snapshot(run_id)

    def _launch(self, run_id: str, *, resumed: bool):
        task = self._tasks.get(run_id)
        if task and not task.done():
            return
        owner = uuid4().hex
        if not self.store.claim_run(run_id, owner):
            return
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id, owner, resumed=resumed))

    async def wait(self, run_id: str):
        task = self._tasks.get(run_id)
        if task:
            await asyncio.shield(task)

    async def close(self):
        """Graceful server shutdown releases leases while keeping recoverable checkpoints."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, run_id: str, owner: str, task: asyncio.Task):
        while True:
            await asyncio.sleep(5)
            if not self.store.renew_owner(run_id, owner):
                task.cancel()
                return
            self.store.expire_approvals(run_id)
            gate = self.service._approval_gate
            if gate:
                for approval in self.store.snapshot(run_id)["approvals"]:
                    if approval["status"] == "expired":
                        gate.respond(approval["pending_id"], ApprovalDecision(False, "审批已过期"))

    async def _drive(self, run_id: str, owner: str, *, resumed: bool):
        heartbeat = asyncio.create_task(self._heartbeat(run_id, owner, asyncio.current_task()))
        t0 = time.perf_counter()
        run = self.store.get_run(run_id)
        self.store.append_event(run_id, "start", {
            "session_id": run["session_id"], "model": self.service.model, "resumed": resumed,
        })
        stream = None
        try:
            messages = await self._rebuild(run_id) if resumed else None
            if resumed and messages is None:
                return
            stream = self.service.stream_run(
                run["session_id"], run["user_input"], existing_run_id=run_id,
                recovered_messages=messages, answer_only=resumed,
                preserve_interruption=True,
            )
            async for event in stream:
                encoded = encode_event(event)
                if encoded:
                    name, data = encoded
                    if name == "approval_pending":
                        approval = self.store.get_approval(data["pending_id"])
                        if approval:
                            data = {**data, "expires_at": approval["expires_at"],
                                    "expected_version": approval["expected_version"],
                                    "arguments_fingerprint": approval["arguments_fingerprint"]}
                    self.store.append_event(run_id, name, data)
                if isinstance(event, LoopEnd):
                    snapshot = self.store.snapshot(run_id)
                    self.store.append_event(run_id, "done", {
                        "steps": event.steps, "completed": snapshot["status"] == "completed",
                        "status": snapshot["status"],
                        "usage": asdict(event.usage) if event.usage else None,
                        "cost": cost_of(self.service.model, event.usage),
                        "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "trace": snapshot["trace_path"],
                    })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.fail_run(run_id, str(exc))
            self.store.append_event(run_id, "error", {"message": str(exc)})
        finally:
            if stream:
                await stream.aclose()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.store.release_owner(run_id, owner)

    async def _rebuild(self, run_id: str) -> list | None:
        run = self.store.get_run(run_id)
        self.store.get_session(run["session_id"])
        messages = copy.deepcopy(run["messages"])
        if messages and messages[-1]["role"] == "assistant" and not messages[-1].get("tool_calls"):
            messages.pop()  # 中断前的最终回答不作为恢复结果；只从工具事实再回答。
        current = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=0)
        prefix, messages = messages[:current], messages[current:]
        tools = {tool.name: tool for tool in self.service._tools}
        results = {
            e["data"]["id"]: e["data"]["content"] for e in self.store.events_after(run_id)
            if e["event"] == "tool_finished"
        }
        results.update({m["tool_call_id"]: m["content"] for m in messages
                        if m["role"] == "tool" and m["tool_call_id"] not in results})
        groups = [m for m in messages if m.get("tool_calls")]
        for group in groups:
            for call in group["tool_calls"]:
                name = call["function"]["name"]
                tool = tools.get(name)
                if (tool and tool.risk and call["id"] not in results
                        and not self.store.get_invocation_by_call(run_id, call["id"])):
                    args = tool.params_model.model_validate_json(call["function"]["arguments"])
                    self.store.record_write_intent(
                        run_id, call["id"], session_id=run["session_id"], tool=name,
                        tool_schema_version=self.service._tool_schema_versions[name],
                        arguments=args.model_dump(mode="json"), client_token=uuid4().hex,
                        pending_id=uuid4().hex[:12], ttl_seconds=self.service._approval_ttl,
                    )
        shown: set[str] = set()
        if self.store.list_invocations(run_id) and self.recovery is None:
            raise RecoveryError("写任务恢复需要业务 token 对账能力，未配置时禁止执行")
        while self.recovery and self.store.list_invocations(run_id):
            self.store.expire_approvals(run_id)
            report = await self.recovery.recover_run(run_id)
            if any(i["invocation_status"] == "unknown" for i in report["invocations"]):
                self.store.append_event(run_id, "run_status", {"status": "recovering",
                                                             "message": "结果待核对"})
                return None
            pending = self.store.list_pending_approvals(run_id)
            if not pending:
                break
            for approval in pending:
                if approval["pending_id"] not in shown:
                    shown.add(approval["pending_id"])
                    self.store.append_event(run_id, "approval_pending", {
                        **approval, "risk": tools[approval["tool"]].risk,
                    })
            await asyncio.sleep(0.1)
        for inv in self.store.list_invocations(run_id):
            content = json.dumps(model_result(inv), ensure_ascii=False)
            if results.get(inv["call_id"]) != content:
                self.store.append_event(run_id, "tool_finished", {
                    "id": inv["call_id"], "name": inv["tool"], "content": content,
                    "ok": inv["status"] == "succeeded", "status": inv["status"],
                })
            approval = self.store.get_approval(inv["approval_pending_id"])
            if (approval and approval["status"] in ("approved", "denied", "expired") and not any(
                e["event"] == "approval_resolved" and
                e["data"]["pending_id"] == approval["pending_id"]
                for e in self.store.events_after(run_id)
            )):
                self.store.append_event(run_id, "approval_resolved", {
                    "call_id": inv["call_id"], "pending_id": approval["pending_id"],
                    "tool": inv["tool"], "approved": approval["status"] == "approved",
                    "reason": approval["decided_reason"] or approval["status"],
                })
        rebuilt = prefix
        for message in messages:
            if message["role"] == "tool":
                continue  # 每组结果下面一次性按 call_id 重建，避免半组/重复回填。
            rebuilt.append(message)
            for call in message.get("tool_calls", []):
                invocation = self.store.get_invocation_by_call(run_id, call["id"])
                if invocation:
                    content = json.dumps(model_result(invocation), ensure_ascii=False)
                elif call["id"] in results:
                    content = results[call["id"]]
                else:
                    tool = tools.get(call["function"]["name"])
                    if tool is None or tool.risk:
                        raise RecoveryError("检查点工具不兼容，禁止重放未知或未审批的写调用")
                    args = tool.params_model.model_validate_json(call["function"]["arguments"])
                    result = await asyncio.wait_for(
                        tool.handler(args), self.service._loop_config.tool_timeout,
                    )
                    content = result if isinstance(result, str) else json.dumps(
                        result, ensure_ascii=False,
                    )
                rebuilt.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        self.store.checkpoint_run(run_id, rebuilt)
        return rebuilt

    async def cancel(self, run_id: str) -> dict:
        self.store.cancel_run(run_id)  # 先使迟到批准无效，再终止执行者。
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.recovery and any(
            inv["status"] in ("unknown", "executing") for inv in self.store.list_invocations(run_id)
        ):
            await self.recovery.recover_run(run_id)
        self.store.append_event(run_id, "run_status", {
            "status": self.store.get_run(run_id)["status"],
        })
        return self.store.snapshot(run_id)

    async def events(self, run_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
        while True:
            batch = self.store.events_after(run_id, after_seq)
            for event in batch:
                after_seq = event["data"]["seq"]
                yield event
            run = self.store.get_run(run_id)
            task = self._tasks.get(run_id)
            # 查询最后一批覆盖执行者收尾与快照/订阅之间的窗口。
            if (not task or task.done()) and not self.store.events_after(run_id, after_seq):
                return
            if run["status"] not in ACTIVE_RUN_STATUSES and not batch:
                return
            await asyncio.sleep(0.05)
