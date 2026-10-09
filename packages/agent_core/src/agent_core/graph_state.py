"""Checkpoint state contains JSON values, never runtime objects."""

from copy import deepcopy
from typing import Any, Literal, TypedDict


class InvocationState(TypedDict, total=False):
    """JSON-safe call and recovery metadata stored in the graph checkpoint."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    risk: str | None
    client_token: str | None
    pending_id: str | None
    tool_schema_version: str
    arguments_fingerprint: str
    approval_created_at: str
    approval_expires_at: str
    approval_status: Literal[
        "pending", "approved", "denied", "expired", "cancelled"
    ]
    invocation_status: Literal[
        "prepared", "waiting_approval", "approved", "executing", "succeeded",
        "failed", "unknown", "denied", "expired", "cancelled"
    ]
    content: str
    ok: bool
    display: dict[str, Any] | None


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    step: int
    usage: dict[str, int] | None
    pending_calls: list[InvocationState]
    tool_results: list[InvocationState]
    tool_history: list[dict[str, Any]]
    completed: bool
    final_answer: str
    recovery_required: bool


def initial_state(messages: list[dict[str, Any]]) -> AgentState:
    return AgentState(
        messages=deepcopy(messages),
        step=0,
        usage=None,
        pending_calls=[],
        tool_results=[],
        tool_history=[],
        completed=False,
        final_answer="",
        recovery_required=False,
    )
