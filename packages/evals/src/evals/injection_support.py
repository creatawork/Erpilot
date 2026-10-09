"""Stable metadata and prompt variants for prompt-injection evaluations."""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from agent_core.demo_tools import TOOL_DATA_TRUST_RULE, system_prompt

from evals.checks import SCORER_VERSION
from evals.model import CaseResult, EvalCase

FIXED_EVALUATION_SEED = 20260930
FIXED_EVALUATION_TIME = "2026-10-07T12:00:00+08:00"


def prompt_for_variant(*, writes_enabled: bool, variant: Literal["control", "treatment"]) -> str:
    """Return the shared prompt, with only the trust rule ablated for control."""
    if variant not in {"control", "treatment"}:
        raise ValueError(f"unknown prompt variant: {variant}")
    prompt = system_prompt(writes_enabled)
    if variant == "treatment":
        if TOOL_DATA_TRUST_RULE not in prompt:
            raise ValueError("shared prompt is missing TOOL_DATA_TRUST_RULE")
        return prompt
    rule_line = f"- {TOOL_DATA_TRUST_RULE}\n"
    if prompt.count(rule_line) != 1:
        raise ValueError("shared prompt must contain TOOL_DATA_TRUST_RULE exactly once")
    return prompt.replace(rule_line, "")


def injection_suite_metadata(
    cases: Sequence[EvalCase],
    results: Sequence[CaseResult],
    case_traces: Mapping[str, str],
    case_snapshots: Mapping[str, object],
    *,
    prompt_variant: Literal["control", "treatment"],
    prompt_sha256: str,
    endpoint_label: str,
    timeout_seconds: float,
    budget_limit_cny: float,
) -> dict[str, object]:
    """Build a credential-free, reproducible `write_report` suite payload."""
    if prompt_variant not in {"control", "treatment"}:
        raise ValueError(f"unknown prompt variant: {prompt_variant}")
    if timeout_seconds <= 0 or budget_limit_cny < 0:
        raise ValueError("timeout must be positive and budget must be non-negative")
    case_ids = [case.id for case in cases]
    case_sha256 = hashlib.sha256(
        "\n".join(case.model_dump_json() for case in cases).encode("utf-8")
    ).hexdigest()
    executed = sum(
        not any(check.startswith(("budget:", "not_run:")) for check in result.failed_checks)
        for result in results
    )
    return {
        "kind": "targeted",
        "case_ids": case_ids,
        "planned": len(cases),
        "executed": executed,
        "scorer_version": SCORER_VERSION,
        "case_sha256": case_sha256,
        "prompt_variant": prompt_variant,
        "prompt_sha256": prompt_sha256,
        "endpoint_label": sanitize_endpoint_label(endpoint_label),
        "timeout_seconds": timeout_seconds,
        "budget_limit_cny": budget_limit_cny,
        "runtime": "evals.runner.run_case",
        "source_changed_during_run": False,
        "seed": {
            "value": FIXED_EVALUATION_SEED,
            "now": FIXED_EVALUATION_TIME,
            "fresh_database_per_case": True,
        },
        "complete": len(results) == len(cases) and executed == len(cases),
        "case_traces": dict(case_traces),
        "case_snapshots": dict(case_snapshots),
    }


def sanitize_endpoint_label(endpoint: str) -> str:
    """Keep endpoint scheme/host/path while removing user info and query data."""
    try:
        parsed = urlsplit(endpoint)
        if not parsed.scheme or not parsed.hostname:
            return "unknown"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "unknown"
