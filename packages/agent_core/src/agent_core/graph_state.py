"""Checkpoint state contains JSON values, never runtime objects."""

from copy import deepcopy
from typing import Any, TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    step: int
    usage: dict[str, int] | None
    pending_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    completed: bool
    final_answer: str


def initial_state(messages: list[dict[str, Any]]) -> AgentState:
    return AgentState(
        messages=deepcopy(messages), step=0, usage=None, pending_calls=[],
        tool_results=[], completed=False, final_answer="",
    )
