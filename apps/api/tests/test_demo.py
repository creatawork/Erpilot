import runpy
from pathlib import Path


async def test_offline_demo_has_verified_approve_deny_quote_and_recovery(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts" / "demo.py"
    demo = runpy.run_path(str(script))
    results = await demo["run_demo"](tmp_path)
    assert {r["scenario"] for r in results} == {"quote", "approve", "deny", "recover"}
    assert all(r["verified"] for r in results)
    assert all(Path(r["trace"]).is_file() for r in results)
