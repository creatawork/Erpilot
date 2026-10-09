# Prompt-injection comparison — pending

This is the plan for a controlled prompt ablation on the current harness, not a historical deployment comparison. **No real-model run was made:** the implementation plan requires an explicit operator-provided `ERPILOT_EVAL_BUDGET`, and none was configured.

| Arm | Prompt | Run report | Status |
|---|---|---|---|
| Control | Shared prompt with only `TOOL_DATA_TRUST_RULE` omitted | — | Not run |
| Treatment | Shared prompt including `TOOL_DATA_TRUST_RULE` | — | Not run |

Both arms are designed to use cases `inj-01` through `inj-04`, model and endpoint from the same operator configuration, seed `20260930`, fixed time `2026-10-07T12:00:00+08:00`, fresh temporary database per case, and the capture-and-deny write gate. Each arm requires the same explicit independent budget cap. Credentials are not written to the report.

## Attack surfaces not compared

| Surface | Case IDs | Status |
|---|---|---|
| Indirect instructions in retrieved fields | `inj-01`, `inj-02` | Not compared |
| Identity or authority spoofing | `inj-03` | Not compared |
| Privilege escalation and false approval | `inj-02`, `inj-03` | Not compared |
| System-prompt disclosure | `inj-01` | Not compared |
| Tool-result poisoning | `inj-04` | Not compared |

Offline prompt-difference, scorer, and report-comparator tests do not establish real-model behavior. No prompt-effectiveness conclusion is available until both matching live reports exist. See the [attack-surface inventory](../../docs/security/prompt-injection-attack-surface.md) for coverage limits.
