"""写评测的业务状态证据：仅允许期望的变更，其他业务行须原样保留。"""

import copy

from erp_store.db import Base
from sqlalchemy import select
from sqlalchemy.engine import Engine

from evals.model import StateExpectation


def snapshot(engine: Engine) -> dict[str, list[dict]]:
    with engine.connect() as connection:
        return {
            name: [dict(row) for row in connection.execute(
                select(Base.metadata.tables[name]).order_by(*Base.metadata.tables[name].primary_key)
            ).mappings()]
            for name in ("products", "stocks", "orders", "order_items")
        }


def check_state(expectation: StateExpectation, before: dict, after: dict) -> list[str]:
    expected = copy.deepcopy(before)
    if expectation.kind != "unchanged":
        table = "stocks" if expectation.kind == "stock_delta" else "products"
        rows = [r for r in expected[table] if r["sku"] == expectation.sku]
        if len(rows) != 1:
            return [f"state: 基线缺少唯一商品/库存 {expectation.sku}"]
        if expectation.kind == "stock_delta":
            rows[0]["quantity"] += expectation.delta
        else:
            rows[0]["status"] = expectation.status
    changed = [name for name in expected if expected[name] != after[name]]
    return [f"state: {expectation.kind} 状态不符：{', '.join(changed)}"] if changed else []
