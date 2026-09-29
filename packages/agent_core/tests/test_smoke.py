import agent_core


def test_package_imports() -> None:
    assert agent_core.__doc__ is not None


def test_loop_stub_documents_goals() -> None:
    from agent_core import loop

    # M1 实现开始后，这里会被真实的 loop 行为测试取代
    assert "agent loop" in loop.__doc__
