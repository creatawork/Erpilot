import json

import pytest
from agent_core.llm import LLMConfig
from agent_core.testing import chunk, make_client, sse_response
from agent_core.tools import Tool
from erpilot_cli import main
from mcp_erp.policy_tools import CANCELLATION_GUIDANCE
from pydantic import BaseModel
from typer.testing import CliRunner


class Params(BaseModel):
    order_id: str


@pytest.mark.parametrize("mode", ["mcp", "demo"])
def test_cli_guidance_matches_available_surface(monkeypatch, tmp_path, mode):
    async def handler(args):
        raise AssertionError("model did not request a tool")

    tools = [Tool("assess_order_cancellation", "query", Params, handler)] if mode == "mcp" else []
    monkeypatch.setattr(main, "_resolve_tools", lambda *args, **kwargs: tools)
    monkeypatch.setattr(main, "_trace_sinks", lambda: [])
    monkeypatch.setattr(main.LLMConfig, "from_env", lambda: LLMConfig(api_key="test"))
    captured = []

    def model(request):
        captured.append(json.loads(request.content)["messages"][0]["content"])
        return sse_response([chunk(delta={"content": "请提供完整订单号。"}), chunk(usage=None)])

    monkeypatch.setattr(main, "LLMClient", lambda config: make_client(model))
    result = CliRunner().invoke(
        main.app, ["chat", "--tools", mode, "--trace-dir", str(tmp_path), "能取消吗"]
    )
    assert result.exit_code == 0, result.output
    assert (CANCELLATION_GUIDANCE in captured[0]) == (mode == "mcp")
    assert "prepare_cancel_order" not in captured[0]
