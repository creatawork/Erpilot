"""Crash a real graph worker at a named checkpoint/ERP boundary.

This file is launched as a child process by test_process_recovery.py. It only
accepts disposable database locations and writes a small marker for its parent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


def _mark(config: dict, **values: object) -> None:
    path = Path(config["marker"])
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


async def _run(config: dict) -> None:
    from agent_core.events import ApprovalPending
    from agent_core.graph_runtime import LangGraphRuntime
    from agent_core.llm import StreamEnd, TextDelta, ToolCall
    from agent_core.tools import Tool
    from erp_store.db import make_engine
    from erp_store.models import ProductStatus
    from erp_store.mutations import ErpMutations
    from erpilot_api.checkpoint import open_checkpointer
    from mcp_erp import build_mutation_reconciler
    from pydantic import BaseModel
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    operation = config["operation"]
    arguments = config["arguments"]
    crash = config.get("crash")
    if operation == "create_order":
        class Params(BaseModel):
            customer: str
            items: list[dict[str, object]]
            note: str | None = None
            client_token: str | None = None
    elif operation == "cancel_order":
        class Params(BaseModel):
            order_id: str
            client_token: str | None = None
    elif operation == "adjust_stock":
        class Params(BaseModel):
            sku: str
            delta: int
            client_token: str | None = None
    else:
        class Params(BaseModel):
            sku: str
            status: str
            client_token: str | None = None

    def count_handler() -> None:
        with Path(config["calls"]).open("a", encoding="utf-8") as handle:
            handle.write("called\n")

    async def handler(args: Params) -> object:
        count_handler()
        engine = make_engine(config["erp_db"])
        try:
            mutations = ErpMutations(engine)
            payload = args.model_dump(exclude_none=True)
            token = payload.pop("client_token")
            if operation == "create_order":
                return mutations.create_order(
                    payload["customer"], payload["items"], note=payload.get("note"),
                    client_token=token,
                ).model_dump(mode="json")
            if operation == "cancel_order":
                result = mutations.cancel_order(payload["order_id"], client_token=token)
                return result.model_dump(mode="json")
            if operation == "adjust_stock":
                sku, quantity = mutations.adjust_stock(
                    payload["sku"], payload["delta"], client_token=token
                )
                return {"sku": sku, "quantity": quantity}
            return {
                "sku": mutations.set_product_status(
                    payload["sku"], ProductStatus(payload["status"]), client_token=token
                ),
                "status": payload["status"],
            }
        finally:
            engine.dispose()

    tool = Tool(operation, "crash-recovery write", Params, handler,
                risk="confirm", retry_safe=True)

    class Client:
        async def stream_chat(self, messages, tools=None):
            if any(message.get("role") == "tool" for message in messages):
                if crash == "before_final_answer":
                    _mark(config, boundary=crash)
                    os._exit(74)
                yield TextDelta("recovered")
            else:
                yield ToolCall("call-write", operation, json.dumps(arguments, ensure_ascii=False))
            yield StreamEnd()

    class CrashBeforeWriteLookup:
        async def lookup(self, tool_name, lookup_arguments, client_token):
            _mark(config, boundary=crash, tool=tool_name, token=client_token)
            os._exit(71)

    if crash in ("before_commit", "after_commit"):
        def terminate_at_boundary(_session):
            _mark(config, boundary=crash)
            os._exit(72 if crash == "before_commit" else 73)

        event.listen(Session, crash, terminate_at_boundary)

    async with open_checkpointer(config["postgres_url"]) as saver:
        reconciler = (
            CrashBeforeWriteLookup()
            if crash == "after_approval_checkpoint"
            else build_mutation_reconciler(config["erp_db"])
        )
        runtime = LangGraphRuntime(
            Client(), [tool], checkpointer=saver, approval_enabled=True,
            mutation_reconciler=reconciler,
            approval_ttl_seconds=config.get("approval_ttl_seconds", 1800),
        )
        if config["stage"] == "prepare":
            events = [event async for event in runtime.stream(
                [{"role": "user", "content": "recover write"}], thread_id=config["thread_id"]
            )]
            pending = next(event for event in events if isinstance(event, ApprovalPending))
            _mark(config, pending_id=pending.pending_id, call_id=pending.call_id,
                  expires_at=pending.expires_at, arguments=pending.arguments)
            os._exit(70)
        snapshot = await runtime.get_state(config["thread_id"])
        pending = [i.value for task in snapshot.tasks for i in task.interrupts]
        if pending:
            events = runtime.resume(
                config["thread_id"],
                {"pending_id": config["pending_id"], "approved": True},
            )
        else:
            events = runtime.continue_run(config["thread_id"])
        async for _event in events:
            pass
        _mark(config, completed=True)


def main() -> None:
    if sys.argv[1] == "smoke":
        os._exit(79)
    asyncio.run(_run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
