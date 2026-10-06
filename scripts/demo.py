"""离线脚本化演示；使用真实 LangGraph/MCP/审批/数据库，不代表真实模型成功率。"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from agent_core.approval import ApprovalDecision, StreamApprovalGate
from agent_core.demo_tools import WRITES_PROMPT
from agent_core.events import ApprovalPending, LoopEnd, ToolCallFinished
from agent_core.graph_runtime import LangGraphRuntime
from agent_core.testing import USAGE, chunk, make_client, sse_response, tool_call_chunks
from agent_core.trace import JsonlTraceRecorder, format_transcript, load_records
from erp_store.db import make_engine
from erp_store.models import ProductStatus
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from langgraph.checkpoint.memory import InMemorySaver
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
            runtime = LangGraphRuntime(
                demo_client(sku), tools=tools, checkpointer=InMemorySaver()
            )
            path = output / f"{scenario}.jsonl"
            # recorder 为追加格式；每次演示使用新的输出目录以保留历史证据。
            recorder = JsonlTraceRecorder(path, "scripted-demo")
            events = []
            async for event in recorder.run(runtime, [
                {"role": "system", "content": WRITES_PROMPT},
                {"role": "user", "content": scenario},
            ], thread_id=f"demo:{scenario}"):
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


def serve(port: int) -> None:
    import uvicorn
    from erpilot_api.main import create_app
    from fastapi.staticfiles import StaticFiles

    with tempfile.TemporaryDirectory(prefix="erpilot-browser-demo-") as directory:
        db = Path(directory) / "erp.db"
        seed_database(db, n_products=60, n_orders=80)
        engine = make_engine(db)
        sku = demo_sku(ErpRepository(engine))
        gate = StreamApprovalGate()
        tools = build_agent_tools(db, writes=True, approval_gate=gate)
        app = create_app(
            client_factory=lambda: demo_client(sku), model="scripted-demo",
            trace_dir=ROOT / "traces" / "browser-demo", tools=tools, approval_gate=gate,
            checkpointer=InMemorySaver(),
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
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        from datetime import datetime
        from uuid import uuid4

        output = ROOT / "traces" / "demo" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        summaries = asyncio.run(run_demo(output))
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(r["verified"] for r in summaries) else 1)


if __name__ == "__main__":
    main()
