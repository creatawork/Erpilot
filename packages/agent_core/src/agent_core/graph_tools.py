"""Validate and execute calls without putting handlers in checkpoint state."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_core.display import build_display
from agent_core.graph_helpers import error_payload


def prepare_tool_calls(calls, tools, *, now=None, approval_ttl_seconds=1800):
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("approval clock must include a timezone")
    if approval_ttl_seconds <= 0:
        raise ValueError("approval TTL must be positive")
    prepared = []
    for call in calls:
        tool = tools.get(call.name)
        record = {
            "call_id": call.id,
            "name": call.name,
            "arguments": {},
            "risk": tool.risk if tool else None,
            "client_token": None,
            "pending_id": None,
        }
        if tool is None:
            record.update(
                content=error_payload("unknown_tool", f"未注册的工具：{call.name}"), ok=False
            )
        else:
            try:
                record["arguments"] = tool.params_model.model_validate_json(
                    call.arguments,
                ).model_dump(mode="json")
                if tool.risk is not None:
                    token = record["arguments"].get("client_token") or uuid4().hex
                    record["client_token"] = token
                    record["pending_id"] = uuid4().hex
                    if "client_token" in tool.params_model.model_fields:
                        record["arguments"]["client_token"] = token
                    schema_json = json.dumps(
                        tool.params_model.model_json_schema(),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    schema_version = hashlib.sha256(
                        schema_json.encode("utf-8")
                    ).hexdigest()
                    business_arguments = {
                        key: value
                        for key, value in record["arguments"].items()
                        if key != "client_token"
                    }
                    fingerprint_json = json.dumps(
                        [tool.name, schema_version, business_arguments],
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    record.update(
                        tool_schema_version=schema_version,
                        arguments_fingerprint=hashlib.sha256(
                            fingerprint_json.encode("utf-8")
                        ).hexdigest(),
                        approval_created_at=now.isoformat(),
                        approval_expires_at=(
                            now + timedelta(seconds=approval_ttl_seconds)
                        ).isoformat(),
                        approval_status="pending",
                        invocation_status="waiting_approval",
                    )
            except Exception as exc:
                record.update(
                    content=error_payload("validation", f"参数校验失败：{exc}"), ok=False
                )
        prepared.append(record)
    return prepared


async def execute_tool_call(call, tools, config):
    if "content" in call:
        return deepcopy(call)
    tool = tools[call["name"]]
    policy = config.retry
    retries = policy.retries if tool.risk is None or tool.retry_safe else 0
    content, ok = "", False
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(policy.backoff * attempt)
        try:
            # Revalidate a fresh copy; a handler must not mutate checkpointed arguments.
            args = tool.params_model.model_validate(deepcopy(call["arguments"]))
            result = await asyncio.wait_for(tool.handler(args), config.tool_timeout)
        except TimeoutError:
            kind = "timeout"
            content = error_payload(kind, f"工具执行超时（>{config.tool_timeout}s）")
        except Exception as exc:
            kind = "execution"
            content = error_payload(kind, f"工具执行出错：{exc}")
        else:
            content = (
                result
                if isinstance(result, str)
                else json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str,
                )
            )
            ok = True
            break
        if kind not in policy.retry_on:
            break
    result = {**deepcopy(call), "content": content, "ok": ok}
    # 展示适配器失败只回退 None，不影响业务结果
    result["display"] = build_display(call["name"], call.get("arguments", {}), content, ok)
    return result
