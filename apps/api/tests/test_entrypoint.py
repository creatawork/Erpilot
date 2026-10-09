from erpilot_api import __main__ as entrypoint


def test_windows_entrypoint_uses_selector_event_loop(monkeypatch):
    policy = object()
    configured = {}

    monkeypatch.setattr(entrypoint.sys, "platform", "win32")
    monkeypatch.setattr(
        entrypoint.asyncio, "WindowsSelectorEventLoopPolicy", lambda: policy, raising=False
    )
    monkeypatch.setattr(
        entrypoint.asyncio, "set_event_loop_policy",
        lambda value: configured.update(policy=value),
    )
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda *args, **kwargs: configured.update(run=kwargs)
    )

    entrypoint.main()

    assert configured["policy"] is policy
    assert configured["run"]["loop"] == "none"
