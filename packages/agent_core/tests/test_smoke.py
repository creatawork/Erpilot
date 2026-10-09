import agent_core
from agent_core.demo_tools import SYSTEM_PROMPT, TOOL_DATA_TRUST_RULE, WRITES_PROMPT


def test_package_imports() -> None:
    assert agent_core.__doc__ is not None


def test_runtime_config_exposes_graph_limits() -> None:
    from agent_core.runtime_config import LoopConfig

    assert LoopConfig().max_steps > 0


def test_tool_data_trust_rule_is_shared_by_read_and_write_prompts() -> None:
    assert TOOL_DATA_TRUST_RULE in SYSTEM_PROMPT
    assert TOOL_DATA_TRUST_RULE in WRITES_PROMPT
