"""Generic recovery lookup contract; concrete ERP adapters live outside agent_core."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

LookupStatus = Literal["found", "absent", "conflict"]


@dataclass(frozen=True, slots=True)
class MutationLookup:
    """Result of checking whether an idempotent side effect already committed."""

    status: LookupStatus
    result: Any | None = None


class MutationReconciler(Protocol):
    async def lookup(
        self, tool_name: str, arguments: dict[str, Any], client_token: str
    ) -> MutationLookup: ...
