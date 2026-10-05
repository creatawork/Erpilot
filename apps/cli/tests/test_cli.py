import json

from agent_core.approval import RISK_SINGLE_CONFIRM
from agent_core.demo_tools import DEMO_TOOLS, WRITES_PROMPT
from agent_core.testing import USAGE, chunk, make_client, sse_response
from agent_core.tools import Tool
from erpilot_cli import main
from typer.testing import CliRunner


def test_cli_writes_uses_write_prompt(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sse_response([chunk(delta={"content": "done"}), chunk(usage=USAGE)])

    original = DEMO_TOOLS[0]
    write = Tool(original.name, original.description, original.params_model,
                 original.handler, risk=RISK_SINGLE_CONFIRM)
    client = make_client(handler)
    monkeypatch.setattr(main.LLMConfig, "from_env", lambda: client.config)
    monkeypatch.setattr(main, "LLMClient", lambda config: client)
    monkeypatch.setattr(main, "build_agent_tools", lambda **kwargs: [write])
    result = CliRunner().invoke(main.app, ["chat", "--writes", "--trace-dir", str(tmp_path), "hi"])
    assert result.exit_code == 0, result.output
    assert requests[0]["messages"][0]["content"] == WRITES_PROMPT
