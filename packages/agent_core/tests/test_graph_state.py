import json

from agent_core.graph_state import initial_state


def test_agent_state_contains_only_serializable_checkpoint_values():
    messages = [{"role": "user", "content": "查库存"}]
    state = initial_state(messages)
    assert json.loads(json.dumps(state, ensure_ascii=False))["messages"] == messages
    assert state["step"] == 0
    assert state["pending_calls"] == []
    state["messages"].append({"role": "assistant", "content": "答复"})
    assert len(messages) == 1
    state["messages"] = [{"role": "user", "content": "下一轮"}]
    assert len(state["messages"]) == 1
