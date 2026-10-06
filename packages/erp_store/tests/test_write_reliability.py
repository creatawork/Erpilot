from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from erp_store import ErpMutations, ErpRepository, MutationError, ProductStatus
from erp_store.db import make_engine
from erp_store.seed import seed_database


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    engine = make_engine(path)
    return path, ErpRepository(engine), ErpMutations(engine)


def available(repo):
    return next(p.sku for p in repo.list_products(status=ProductStatus.ON_SALE, limit=60)
                if repo.get_stock(p.sku).quantity >= 2)


def test_order_token_replays_after_restart_and_conflicts_on_changed_payload(store):
    path, repo, mutations = store
    sku = available(repo)
    before = repo.get_stock(sku).quantity
    first = mutations.create_order("test", [(sku, 2)], client_token="order-1")
    restarted = ErpMutations(make_engine(path))
    replay = restarted.create_order("test", [(sku, 1), (sku, 1)], client_token="order-1")
    assert replay == first
    assert repo.get_stock(sku).quantity == before - 2
    assert repo.count_orders(customer="test") == 1
    with pytest.raises(MutationError, match="幂等") as exc:
        restarted.create_order("test", [(sku, 1)], client_token="order-1")
    assert exc.value.code == "idempotency_conflict"


def test_every_write_replays_original_result_once(store):
    _, repo, mutations = store
    sku = available(repo)
    original = repo.get_stock(sku).quantity
    first = mutations.adjust_stock(sku, 5, client_token="stock-1")
    assert mutations.adjust_stock(sku, 5, client_token="stock-1") == first
    assert repo.get_stock(sku).quantity == original + 5
    order = mutations.create_order("test", [(sku, 2)], client_token="order-1")
    cancelled = mutations.cancel_order(order.order_id, client_token="cancel-1")
    assert mutations.cancel_order(order.order_id, client_token="cancel-1") == cancelled
    assert repo.get_stock(sku).quantity == original + 5
    assert mutations.set_product_status(sku, ProductStatus.OFF_SALE, client_token="status-1") == sku
    assert mutations.set_product_status(sku, ProductStatus.OFF_SALE, client_token="status-1") == sku


def parallel(n, operation):
    barrier = Barrier(n)

    def run(i):
        barrier.wait(timeout=10)
        return operation(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_parallel_stock_adjustments_have_no_lost_updates(store):
    path, repo, _ = store
    sku = available(repo)
    before = repo.get_stock(sku).quantity
    parallel(12, lambda i: ErpMutations(make_engine(path)).adjust_stock(sku, 1))
    assert repo.get_stock(sku).quantity == before + 12


def test_parallel_orders_cannot_oversell_or_collide_on_order_id(store):
    path, repo, mutations = store
    sku = available(repo)
    mutations.adjust_stock(sku, 3 - repo.get_stock(sku).quantity)

    def order(i):
        try:
            return ErpMutations(make_engine(path)).create_order(f"buyer-{i}", [(sku, 1)])
        except MutationError as exc:
            assert exc.code == "insufficient_stock"
            return None

    results = [r for r in parallel(8, order) if r is not None]
    assert len(results) == 3
    assert len({r.order_id for r in results}) == 3
    assert repo.get_stock(sku).quantity == 0


def test_parallel_same_token_creates_one_order(store):
    path, repo, _ = store
    sku = available(repo)
    before = repo.get_stock(sku).quantity
    orders = parallel(6, lambda i: ErpMutations(make_engine(path)).create_order(
        "same", [(sku, 1)], client_token="one-request",
    ))
    assert len({o.order_id for o in orders}) == 1
    assert repo.count_orders(customer="same") == 1
    assert repo.get_stock(sku).quantity == before - 1


# ---- 按 token 对账的内部查询（T06，ADR-0008 §3 推演 3） ----


def test_lookup_token_distinguishes_found_not_found_and_conflict(store):
    _, repo, mutations = store
    sku = available(repo)

    # 查询成功、无记录：不单独证明未执行
    assert mutations.lookup_token("no-such-token").status == "not_found"

    result = mutations.adjust_stock(sku, 3, client_token="reconcile-1")
    found = mutations.lookup_token("reconcile-1")
    assert found.status == "found"
    assert found.result == [sku, repo.get_stock(sku).quantity]
    assert result == (sku, repo.get_stock(sku).quantity)

    # 同 token 异参：conflict，结果不给（原批准不可迁移到异参动作）
    conflict = mutations.lookup_token("reconcile-1")
    assert conflict.status == "found"
    assert ErpMutations.canonical_request("adjust_stock", {"sku": sku, "delta": 1}) \
        != conflict.request


def test_lookup_token_rejects_malformed_key(store):
    _, _, mutations = store
    with pytest.raises(MutationError, match="幂等键"):
        mutations.lookup_token("")


def test_canonical_request_matches_persisted_request_for_all_four_tools(store):
    """canonical_request 是幂等表 request 的逆映射：对账靠它判同 token 异参。"""
    path, repo, mutations = store
    sku = available(repo)
    mutations.adjust_stock(sku, 2, client_token="canon-1")
    mutations.set_product_status(sku, ProductStatus.OFF_SALE, client_token="canon-2")
    mutations.set_product_status(sku, ProductStatus.ON_SALE, client_token="canon-3")
    order = mutations.create_order("对账客户", [(sku, 1), (sku, 1)], client_token="canon-4")
    mutations.cancel_order(order.order_id, client_token="canon-5")

    import json

    from sqlalchemy import create_engine, text

    with create_engine(f"sqlite:///{path}").connect() as conn:
        rows = dict(conn.execute(
            text("SELECT client_token, request FROM mutation_requests")
        ).all())

    assert json.loads(rows["canon-1"]) == ErpMutations.canonical_request(
        "adjust_stock", {"sku": sku, "delta": 2}
    )
    assert json.loads(rows["canon-2"]) == ErpMutations.canonical_request(
        "set_product_status", {"sku": sku, "status": "已下架"}
    )
    assert json.loads(rows["canon-4"]) == ErpMutations.canonical_request(
        "create_order", {"customer": " 对账客户 ", "items": [
            {"sku": sku, "quantity": 1}, {"sku": sku, "quantity": 1},
        ]}
    )
    assert json.loads(rows["canon-5"]) == ErpMutations.canonical_request(
        "cancel_order", {"order_id": order.order_id}
    )
    with pytest.raises(ValueError, match="口径"):
        ErpMutations.canonical_request("get_order", {"order_id": "x"})
