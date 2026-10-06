"""离线脚本化演示；使用真实 loop/MCP/审批/数据库，不代表真实模型成功率。"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import WRITES_PROMPT
from agent_core.loop import AgentLoop, ApprovalPending, LoopEnd, ToolCallFinished
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.trace import JsonlTraceRecorder, format_transcript, load_records
from erp_store.db import make_engine
from erp_store.models import ProductStatus
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from mcp_erp import build_agent_tools, build_agent_tools_async

ROOT = Path(__file__).resolve().parents[1]


def demo_client(sku: str):
    def handler(request):
        messages = json.loads(request.content)["messages"]
        user_index = max(i for i, m in enumerate(messages) if m["role"] == "user")
        question = messages[user_index]["content"]
        results = [json.loads(m["content"]) for m in messages[user_index + 1:]
                   if m["role"] == "tool"]
        quote = "quote" in question or "报价" in question
        recover = "recover" in question or "恢复" in question
        if not results:
            name = "compute_quote" if quote else "adjust_stock"
            args = {"sku": sku, "quantity": 10} if quote else {
                "sku": sku, "delta": -100000 if recover else 5,
            }
            return sse_response(tool_call_chunks("demo-1", name, json.dumps(args)))
        if recover and len(results) == 1:
            return sse_response(tool_call_chunks("demo-2", "get_stock", json.dumps({"sku": sku})))
        last = results[-1]
        if last.get("approval") == "denied":
            text = "审批已拒绝，操作未执行，库存没有变化。"
        elif recover:
            text = f"出库失败：库存不足。已复核当前库存 {last['quantity']} 件，请减少数量。"
        elif quote:
            text = f"10 件报价为 {last['total']} 元，单价 {last['unit_price']} 元，以工具结果为准。"
        else:
            text = f"库存已增加 5 件，当前库存 {last['quantity']} 件。"
        return sse_response([chunk(delta={"content": text}), chunk(usage=USAGE)])

    return make_client(handler)


def demo_sku(repo: ErpRepository) -> str:
    return next(p.sku for p in repo.list_products(status=ProductStatus.ON_SALE, limit=60)
                if repo.get_stock(p.sku).quantity >= 10)


async def run_demo(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    with tempfile.TemporaryDirectory(prefix="erpilot-demo-") as directory:
        for scenario in ("quote", "approve", "deny", "recover"):
            db = Path(directory) / f"{scenario}.db"
            seed_database(db, n_products=60, n_orders=80)
            engine = make_engine(db)
            repo = ErpRepository(engine)
            sku = demo_sku(repo)
            before = repo.get_stock(sku).quantity
            gate = StreamApprovalGate()
            tools = await build_agent_tools_async(db, writes=True, approval_gate=gate)
            loop = AgentLoop(demo_client(sku), tools=tools)
            path = output / f"{scenario}.jsonl"
            # recorder 为追加格式；每次演示使用新的输出目录以保留历史证据。
            recorder = JsonlTraceRecorder(path, "scripted-demo")
            events = []
            async for event in recorder.run(loop, [
                {"role": "system", "content": WRITES_PROMPT},
                {"role": "user", "content": scenario},
            ]):
                events.append(event)
                if isinstance(event, ApprovalPending):
                    gate.respond(event.pending_id, ApprovalDecision(scenario != "deny", "演示决策"))
            after = repo.get_stock(sku).quantity
            completed = isinstance(events[-1], LoopEnd) and events[-1].completed
            delta = 5 if scenario == "approve" else 0
            verified = completed and after == before + delta and not gate._waiters
            if scenario == "recover":
                calls = [e for e in events if isinstance(e, ToolCallFinished)]
                error_code = json.loads(calls[0].content)["error"]["code"]
                verified = verified and error_code == "insufficient_stock"
                verified = verified and calls[-1].name == "get_stock"
            if scenario == "quote":
                calls = [e for e in events if isinstance(e, ToolCallFinished)]
                total = json.loads(calls[0].content)["total"]
                verified = verified and total == repo.compute_quote(sku, 10).total
            summary = {
                "scenario": scenario, "sku": sku, "before": before, "after": after,
                "verified": verified, "trace": str(path.resolve()), "source": "scripted-demo",
            }
            summaries.append(summary)
            (output / f"{scenario}.md").write_text(
                format_transcript(load_records(path)), encoding="utf-8",
            )
            engine.dispose()
    (output / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summaries


async def run_recovery_demo(output: Path) -> list[dict]:
    """重建运行存储后继续原任务；脚本化批准，不代表真人验收。"""
    from uuid import uuid4

    from erp_store import ErpMutations
    from erpilot_api.execution import RunManager
    from erpilot_api.recovery import build_erp_recovery
    from erpilot_api.run_store import RunStore
    from erpilot_api.service import ChatService

    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    with tempfile.TemporaryDirectory(prefix="erpilot-recovery-demo-") as directory:
        for scenario in ("pending_restart", "committed_restart", "denied_restart"):
            db = Path(directory) / f"{scenario}.db"
            seed_database(db, n_products=60, n_orders=80)
            engine = make_engine(db)
            repo = ErpRepository(engine)
            sku = demo_sku(repo)
            before = repo.get_stock(sku).quantity
            gate = StreamApprovalGate()
            token = uuid4().hex
            tools = await build_agent_tools_async(
                db, writes=True, approval_gate=gate, token_factory=lambda: uuid4().hex,
            )
            store = RunStore(Path(directory) / f"{scenario}-runs.db")
            trace = output / f"{scenario}.jsonl"
            run = store.create_run("demo", "approve", [], str(trace))
            args = {"sku": sku, "delta": 5}
            inv = store.record_write_intent(
                run, "demo-1", session_id="demo", tool="adjust_stock",
                tool_schema_version=next(
                    t.schema_version for t in tools if t.name == "adjust_stock"
                ),
                arguments=args, client_token=token, pending_id="demo-pending", ttl_seconds=1800,
            )
            store.checkpoint_run(run, [
                {"role": "system", "content": WRITES_PROMPT},
                {"role": "user", "content": "approve"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "demo-1", "type": "function",
                    "function": {"name": "adjust_stock", "arguments": json.dumps(args)},
                }]},
            ])
            if scenario == "committed_restart":
                store.decide_approval("demo-pending", True, "脚本化演示批准")
                ErpMutations(engine).adjust_stock(sku, 5, client_token=token)
                store.set_invocation_status(inv, "executing")
            reopened = RunStore(store.path)
            service = ChatService(
                client_factory=lambda sku=sku: demo_client(sku), model="scripted-demo", tools=tools,
                trace_dir=output, run_store=reopened, approval_gate=gate,
            )
            runs = RunManager(service, reopened, build_erp_recovery(reopened, str(db), tools))
            await runs.resume(run)
            if scenario != "committed_restart":
                async with asyncio.timeout(5):
                    while not reopened.events_after(run):
                        await asyncio.sleep(0.01)
                service.respond_approval("demo-pending", ApprovalDecision(
                    scenario != "denied_restart", "脚本化演示决定",
                ))
            await runs.wait(run)
            snap = reopened.snapshot(run)
            after = repo.get_stock(sku).quantity
            lookup = ErpMutations(engine).lookup_token(token)
            expected = 0 if scenario == "denied_restart" else 5
            verified = snap["status"] == "completed" and after == before + expected
            verified = verified and lookup.status == ("not_found" if expected == 0 else "found")
            summary = {
                "scenario": scenario, "run_id": run, "token": token, "before": before,
                "after": after, "verified": verified, "trace": str(trace.resolve()),
                "source": "scripted-recovery-demo", "snapshot": snap,
            }
            summaries.append(summary)
            engine.dispose()
    (output / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summaries


def serve(port: int) -> None:
    from uuid import uuid4

    import uvicorn
    from erpilot_api.main import create_app
    from fastapi.staticfiles import StaticFiles

    with tempfile.TemporaryDirectory(prefix="erpilot-browser-demo-") as directory:
        db = Path(directory) / "erp.db"
        seed_database(db, n_products=60, n_orders=80)
        engine = make_engine(db)
        sku = demo_sku(ErpRepository(engine))
        gate = StreamApprovalGate()
        tools = build_agent_tools(
            db, writes=True, approval_gate=gate, token_factory=lambda: uuid4().hex,
        )
        app = create_app(
            client_factory=lambda: demo_client(sku), model="scripted-demo",
            trace_dir=ROOT / "traces" / "browser-demo", tools=tools, approval_gate=gate,
            run_store_path=Path(directory) / "runs.db", erp_db_path=db,
        )
        app.mount("/", StaticFiles(directory=ROOT / "apps/web/dist", html=True))
        try:
            uvicorn.run(app, host="127.0.0.1", port=port)
        finally:
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--recovery", action="store_true", help="追加三种重启恢复演示")
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        from datetime import datetime
        from uuid import uuid4

        output = ROOT / "traces" / "demo" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        summaries = asyncio.run(run_demo(output))
        if args.recovery:
            summaries.extend(asyncio.run(run_recovery_demo(output / "recovery")))
        print(json.dumps([{k: v for k, v in r.items() if k != "snapshot"} for r in summaries],
                         ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(r["verified"] for r in summaries) else 1)


if __name__ == "__main__":
    main()
