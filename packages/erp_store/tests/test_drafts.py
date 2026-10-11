from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from erp_store.db import ProductRow, StockRow, init_db, make_engine
from erp_store.drafts import ErpDrafts
from erp_store.models import ProductStatus
from erp_store.mutations import MutationError
from erp_store.repository import ErpRepository
from sqlalchemy.orm import Session


@pytest.fixture
def store(tmp_path):
    engine = make_engine(tmp_path / "erp.db")
    init_db(engine)
    with Session(engine) as session:
        for sku, price in [("A", 100.0), ("B", 20.0)]:
            session.add(
                ProductRow(
                    sku=sku,
                    name=sku,
                    category="测试",
                    price=price,
                    status=ProductStatus.ON_SALE.value,
                )
            )
            session.add(StockRow(sku=sku, quantity=1000, warehouse="主仓"))
        session.commit()
    return engine, ErpRepository(engine), ErpDrafts(engine)


def test_prepare_merges_and_prices_without_business_write(store):
    _, repo, drafts = store
    draft = drafts.prepare("s1", "客户", [("A", 6), ("B", 5), ("A", 4)])
    assert draft["revision"] == 1 and draft["status"] == "open"
    assert draft["total_amount"] == 1080.0 and draft["order_id"] is None
    assert len(draft["items"]) == 2
    assert draft["items"][0] == dict(
        sku="A",
        name="A",
        quantity=10,
        base_price=100.0,
        discount=0.98,
        unit_price=98.0,
        line_total=980.0,
        stock_before=1000,
        stock_after=990,
    )
    assert repo.get_stock("A").quantity == 1000
    assert repo.count_orders(customer="客户") == 0


def test_revision_is_immutable_and_old_version_cannot_submit(store):
    _, _, drafts = store
    first = drafts.prepare("s1", "客户", [("A", 9)])
    second = drafts.revise("s1", first["draft_id"], 1, "改后客户", [("A", 10)])
    assert second["revision"] == 2 and second["total_amount"] == 980.0
    assert drafts.get("s1", first["draft_id"], 1)["total_amount"] == 900.0
    assert drafts.get("s1", first["draft_id"], 1)["status"] == "superseded"
    with pytest.raises(MutationError) as error:
        drafts.submit("s1", first["draft_id"], 1, "old-approval")
    assert error.value.code == "draft_changed"
    with pytest.raises(MutationError) as error:
        drafts.revise("s1", first["draft_id"], 1, "客户", [("A", 1)])
    assert error.value.code == "draft_changed"
    assert drafts.list_latest("s1")[0]["revision"] == 2


@pytest.mark.parametrize("operation", ["get", "revise", "submit"])
def test_cross_session_access_is_not_found(store, operation):
    _, repo, drafts = store
    draft = drafts.prepare("s1", "私有客户", [("A", 10)])
    args = {"get": (), "revise": (1, "越界客户", [("A", 10)]), "submit": (1, "forged-token")}[
        operation
    ]
    with pytest.raises(MutationError) as error:
        getattr(drafts, operation)("s2", draft["draft_id"], *args)
    assert error.value.code == "not_found"
    assert drafts.list_latest("s2") == []
    assert repo.get_stock("A").quantity == 1000


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("price", 200.0, "quote_changed"),
        ("quantity", 999, "stock_changed"),
        ("quantity", 1001, "stock_changed"),
        ("status", ProductStatus.OFF_SALE.value, "quote_changed"),
    ],
)
def test_conditions_change_requires_fresh_revision(store, field, value, code):
    engine, repo, drafts = store
    draft = drafts.prepare("s1", "客户", [("A", 10)])
    with Session(engine) as session:
        row = session.get(StockRow if field == "quantity" else ProductRow, "A")
        setattr(row, field, value)
        session.commit()
    before = repo.get_stock("A").quantity
    with pytest.raises(MutationError) as error:
        drafts.submit("s1", draft["draft_id"], 1, "approved")
    assert error.value.code == code
    assert repo.count_orders(customer="客户") == 0
    assert repo.get_stock("A").quantity == before


def test_expired_draft_refreshes_as_new_version(store):
    engine, repo, _ = store
    now = datetime(2026, 10, 11, tzinfo=UTC)
    drafts = ErpDrafts(engine, clock=lambda: now)
    draft = drafts.prepare("s1", "客户", [("A", 10)])
    now += timedelta(minutes=30)
    assert drafts.get("s1", draft["draft_id"])["status"] == "expired"
    with pytest.raises(MutationError) as error:
        drafts.submit("s1", draft["draft_id"], 1, "expired")
    assert error.value.code == "draft_expired"
    revised = drafts.revise("s1", draft["draft_id"], 1, "客户", [("A", 10)])
    assert revised["status"] == "open" and revised["revision"] == 2
    assert drafts.submit("s1", draft["draft_id"], 2, "fresh").total_amount == 980.0
    assert repo.get_stock("A").quantity == 990


def test_discount_change_invalidates_even_when_unit_price_rounds_the_same(store, monkeypatch):
    _, repo, drafts = store
    draft = drafts.prepare("s1", "客户", [("A", 10)])
    monkeypatch.setattr(
        "erp_store.drafts.price_for_quantity", lambda price, qty: (0.98001, 98.0, 980.0)
    )
    with pytest.raises(MutationError) as error:
        drafts.submit("s1", draft["draft_id"], 1, "approval")
    assert error.value.code == "quote_changed"
    assert repo.count_orders(customer="客户") == 0


def test_same_and_new_tokens_replay_one_order_and_disallow_edit(store):
    engine, repo, drafts = store
    draft = drafts.prepare("s1", "客户", [("A", 10), ("B", 2)])
    first = drafts.submit("s1", draft["draft_id"], 1, "first")
    restarted = ErpDrafts(engine)
    assert restarted.submit("s1", draft["draft_id"], 1, "first") == first
    assert restarted.submit("s1", draft["draft_id"], 1, "different-token") == first
    assert repo.count_orders(customer="客户") == 1
    assert repo.get_stock("A").quantity == 990 and repo.get_stock("B").quantity == 998
    assert restarted.get("s1", draft["draft_id"])["order_id"] == first.order_id
    with pytest.raises(MutationError) as error:
        restarted.revise("s1", draft["draft_id"], 1, "客户", [("A", 1)])
    assert error.value.code == "draft_submitted"
    args = {"draft_id": draft["draft_id"], "revision": 1}
    assert restarted.lookup_submission("s1", args, "first") == (
        "found",
        first.model_dump(mode="json"),
    )
    assert restarted.lookup_submission("s2", args, "first") == ("absent", None)


def test_token_conflicts_across_drafts(store):
    _, repo, drafts = store
    first = drafts.prepare("s1", "客户", [("A", 10)])
    second = drafts.prepare("s1", "客户", [("B", 2)])
    drafts.submit("s1", first["draft_id"], 1, "same-token")
    with pytest.raises(MutationError) as error:
        drafts.submit("s1", second["draft_id"], 1, "same-token")
    assert error.value.code == "idempotency_conflict"
    assert repo.get_stock("B").quantity == 1000


def test_new_token_replays_original_submission_even_after_cancellation(store):
    _, repo, drafts = store
    draft = drafts.prepare("s1", "客户", [("A", 10)])
    first = drafts.submit("s1", draft["draft_id"], 1, "first")
    drafts.cancel_order(first.order_id, client_token="cancel")
    assert repo.get_stock("A").quantity == 1000
    assert drafts.submit("s1", draft["draft_id"], 1, "again") == first
    assert repo.count_orders(customer="客户") == 1


def test_parallel_new_tokens_submit_single_order(store):
    engine, repo, drafts = store
    draft = drafts.prepare("s1", "并发客户", [("A", 10)])
    with ThreadPoolExecutor(max_workers=4) as pool:
        orders = list(
            pool.map(
                lambda i: ErpDrafts(engine).submit("s1", draft["draft_id"], 1, f"token-{i}"),
                range(4),
            )
        )
    assert len({order.order_id for order in orders}) == 1
    assert repo.count_orders(customer="并发客户") == 1
    assert repo.get_stock("A").quantity == 990


@pytest.mark.parametrize("items", [[], [("A", 0)], [("A", True)], [("A", 1.5)]])
def test_invalid_items_do_not_create_draft(store, items):
    _, _, drafts = store
    with pytest.raises(MutationError) as error:
        drafts.prepare("s1", "客户", items)
    assert error.value.code == "invalid_argument"
    assert drafts.list_latest("s1") == []
