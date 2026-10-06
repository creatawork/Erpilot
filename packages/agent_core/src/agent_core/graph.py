"""LangGraph orchestration using the existing OpenAI-compatible client."""

import asyncio
import time
from dataclasses import asdict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_core.approval import ApprovalDecision, _denial_payload
from agent_core.context import compress_messages
from agent_core.events import (
    ApprovalResolved,
    StepEnd,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from agent_core.graph_approval import ResumeDecision, make_approval_payload
from agent_core.graph_helpers import assistant_toolcall_message, merge_usage
from agent_core.graph_state import AgentState
from agent_core.graph_tools import execute_tool_call, prepare_tool_calls
from agent_core.llm import StreamEnd, TextDelta, ToolCall, Usage


def build_graph(client, tools, config, checkpointer, *, approval_enabled=False):
    registry = {tool.name: tool for tool in tools}
    if len(registry) != len(tools):
        raise ValueError("工具重名")
    if any(t.risk is not None for t in tools) and not approval_enabled:
        raise ValueError("write tools require approval runtime")

    async def model(state):
        writer = get_stream_writer()
        step = state["step"] + 1
        writer(StepStarted(step))
        started = time.perf_counter()
        messages = list(state["messages"])
        compress_messages(messages, config.context)
        parts, calls, usage = [], [], None
        async for event in client.stream_chat(
            messages,
            tools=[t.openai_schema() for t in tools] or None,
        ):
            if isinstance(event, TextDelta):
                parts.append(event.text)
                writer(event)
            elif isinstance(event, ToolCall):
                calls.append(event)
            elif isinstance(event, StreamEnd):
                usage = event.usage
        writer(StepEnd(step, usage, round((time.perf_counter() - started) * 1000, 1)))
        text = "".join(parts)
        messages.append(
            assistant_toolcall_message(text, calls)
            if calls
            else {
                "role": "assistant",
                "content": text,
            }
        )
        total = merge_usage(Usage(**state["usage"]) if state["usage"] else None, usage)
        return {
            "messages": messages,
            "step": step,
            "usage": asdict(total) if total else None,
            "pending_calls": [asdict(call) for call in calls],
            "tool_results": [],
            "completed": not calls,
            "final_answer": text if not calls else "",
        }

    def prepare(state):
        calls = [ToolCall(**call) for call in state["pending_calls"]]
        writer = get_stream_writer()
        for call in calls:
            writer(ToolCallStarted(call))
        return {"pending_calls": prepare_tool_calls(calls, registry)}

    async def reads(state):
        writer = get_stream_writer()

        async def execute(call):
            result = await execute_tool_call(call, registry, config)
            writer(
                ToolCallFinished(result["call_id"], result["name"], result["content"], result["ok"])
            )
            return result

        results = await asyncio.gather(
            *(
                execute(call)
                for call in state["pending_calls"]
                if call["risk"] is None or "content" in call
            )
        )
        return {"tool_results": results}

    def next_write(state):
        done = {r["call_id"] for r in state["tool_results"]}
        return next((c for c in state["pending_calls"] if c["call_id"] not in done), None)

    async def write(state):
        call = next_write(state)
        decision = ResumeDecision.model_validate(interrupt(make_approval_payload(call)))
        if decision.pending_id != call["pending_id"]:
            raise ValueError("stale approval pending_id")
        writer = get_stream_writer()
        writer(
            ApprovalResolved(
                call["call_id"],
                call["pending_id"],
                call["name"],
                decision.approved,
                decision.reason,
            )
        )
        if decision.approved:
            result = await execute_tool_call(call, registry, config)
        else:
            result = {
                **call,
                "ok": True,
                "content": _denial_payload(
                    ApprovalDecision(False, decision.reason),
                ),
            }
        writer(ToolCallFinished(result["call_id"], result["name"], result["content"], result["ok"]))
        return {"tool_results": [*state["tool_results"], result]}

    def collect(state):
        results = {r["call_id"]: r for r in state["tool_results"]}
        calls = {c["call_id"]: c for c in state["pending_calls"]}
        return {
            "messages": [
                *state["messages"],
                *[
                    {
                        "role": "tool",
                        "tool_call_id": call["call_id"],
                        "content": results[call["call_id"]]["content"],
                    }
                    for call in state["pending_calls"]
                ],
            ],
            "tool_history": [
                *state["tool_history"],
                *[
                    {
                        "call_id": result["call_id"],
                        "name": result["name"],
                        "arguments": calls[result["call_id"]]["arguments"],
                        "content": result["content"],
                        "ok": result["ok"],
                    }
                    for result in state["tool_results"]
                ],
            ],
            "pending_calls": [],
        }

    graph = StateGraph(AgentState)
    graph.add_node("model", model)
    graph.add_node("prepare", prepare)
    graph.add_node("reads", reads)
    graph.add_node("write", write)
    graph.add_node("collect", collect)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", lambda s: END if s["completed"] else "prepare")
    graph.add_edge("prepare", "reads")
    graph.add_conditional_edges("reads", lambda s: "write" if next_write(s) else "collect")
    graph.add_conditional_edges("write", lambda s: "write" if next_write(s) else "collect")
    graph.add_conditional_edges(
        "collect", lambda s: END if s["step"] >= config.max_steps else "model"
    )
    return graph.compile(checkpointer=checkpointer)
