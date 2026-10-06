import agent_core


def test_package_imports() -> None:
    assert agent_core.__doc__ is not None


def test_runtime_config_exposes_graph_limits() -> None:
    from agent_core.runtime_config import LoopConfig

    assert LoopConfig().max_steps > 0
