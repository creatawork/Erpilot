"""Validate and execute calls without putting handlers in checkpoint state."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

from agent_core.loop import _error_payload


def prepare_tool_calls(calls, tools):
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
                content=_error_payload("unknown_tool", f"未注册的工具：{call.name}"), ok=False
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
            except Exception as exc:
                record.update(
                    content=_error_payload("validation", f"参数校验失败：{exc}"), ok=False
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
            content = _error_payload(kind, f"工具执行超时（>{config.tool_timeout}s）")
        except Exception as exc:
            kind = "execution"
            content = _error_payload(kind, f"工具执行出错：{exc}")
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
    return {**deepcopy(call), "content": content, "ok": ok}
