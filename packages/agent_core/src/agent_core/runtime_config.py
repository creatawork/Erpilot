"""Shared limits and retry policy for the graph runtime."""

from dataclasses import dataclass, field

from agent_core.context import ContextPolicy


@dataclass(frozen=True, slots=True)
class ToolRetryPolicy:
    """Retry transient tool failures; deterministic input errors are never retried."""

    retries: int = 1
    backoff: float = 0.5
    retry_on: frozenset[str] = frozenset({"timeout", "execution"})


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_steps: int = 8
    tool_timeout: float = 30.0
    retry: ToolRetryPolicy = field(default_factory=ToolRetryPolicy)
    context: ContextPolicy = field(default_factory=ContextPolicy)
