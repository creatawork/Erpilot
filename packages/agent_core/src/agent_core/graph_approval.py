"""Fixed approval payload and strict decision validation."""

from copy import deepcopy

from pydantic import BaseModel, ConfigDict, StrictBool


class ResumeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending_id: str
    approved: StrictBool
    reason: str = ""


def make_approval_payload(call):
    return {
        "call_id": call["call_id"],
        "pending_id": call["pending_id"],
        "tool": call["name"],
        "risk": call["risk"],
        "arguments": deepcopy(call["arguments"]),
    }
