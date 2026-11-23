from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from mock_commerce import seed
from mock_commerce.db import make_session_factory
from mock_commerce.models import Customer, Order, Subscription, as_utc
from mock_commerce.seed import (
    FIXTURE_DELIVERED_10_DAYS,
    FIXTURE_DELIVERED_45_DAYS,
    FIXTURE_PARTIAL_REFUND,
    FIXTURE_SHARED_PHONE,
    FIXTURE_SHARED_PHONE_CUSTOMERS,
    SEED_DIR,
    generate_seed_data,
    read_seed_files,
    reset_and_seed,
)


def test_seed_lives_in_data_seed() -> None:
    assert Path(__file__).resolve().parents[2] / "data" / "seed" == SEED_DIR
    for name in ("customers", "orders", "subscriptions", "loyalty"):
        assert (SEED_DIR / f"{name}.json").is_file()


def test_generator_is_deterministic() -> None:
    assert generate_seed_data() == generate_seed_data()


def test_committed_files_match_generator() -> None:
    # If this fails, someone edited the generator without regenerating:
    #   uv run python -m mock_commerce.seed --generate
    assert read_seed_files() == generate_seed_data()


def test_stable_ids_and_sizes() -> None:
    data = read_seed_files()
    assert len(data.customers) == seed.N_CUSTOMERS == 50
    assert len(data.orders) == seed.N_ORDERS == 200
    assert len(data.subscriptions) == seed.N_SUBSCRIPTIONS == 20
    assert {loyalty.customer_id for loyalty in data.loyalty} == {c.id for c in data.customers}
    assert data.customers[0].id == "cus_0001"
    assert data.customers[-1].id == "cus_0050"
    assert data.orders[0].order_number == "NW-10001"
    assert data.orders[-1].order_number == "NW-10200"
    assert len({o.order_number for o in data.orders}) == 200
    assert len({c.email for c in data.customers}) == 50


def test_every_order_status_is_represented() -> None:
    counts = Counter(o.status for o in read_seed_files().orders)
    assert set(counts) == {"processing", "shipped", "delivered", "returned", "cancelled"}
    assert all(n >= 5 for n in counts.values())


def test_tricky_cases_exist() -> None:
    data = read_seed_files()
    orders = {o.order_number: o for o in data.orders}

    outside = orders[FIXTURE_DELIVERED_45_DAYS]
    assert (outside.status, outside.delivered_days_ago) == ("delivered", 45)

    inside = orders[FIXTURE_DELIVERED_10_DAYS]
    assert (inside.status, inside.delivered_days_ago) == ("delivered", 10)

    partial = orders[FIXTURE_PARTIAL_REFUND]
    refunded = sum(r.amount_cents for r in partial.refunds)
    assert 0 < refunded < partial.total_cents

    sharing = sorted(c.id for c in data.customers if c.phone == FIXTURE_SHARED_PHONE)
    assert tuple(sharing) == FIXTURE_SHARED_PHONE_CUSTOMERS
    assert Counter(c.phone for c in data.customers).most_common(1)[0][1] == 2  # only that pair


def test_order_timelines_are_consistent() -> None:
    for o in read_seed_files().orders:
        if o.status in {"shipped", "delivered", "returned"}:
            assert o.shipped_days_ago is not None
            assert o.shipped_days_ago <= o.placed_days_ago
            assert o.tracking_number
        if o.status in {"delivered", "returned"}:
            assert o.delivered_days_ago is not None
            assert o.shipped_days_ago is not None
            assert o.delivered_days_ago <= o.shipped_days_ago
        if o.status in {"processing", "cancelled"}:
            assert o.shipped_days_ago is None
        if o.status in {"cancelled", "returned"}:
            assert sum(r.amount_cents for r in o.refunds) == o.total_cents
        assert sum(r.amount_cents for r in o.refunds) <= o.total_cents


def test_load_resolves_offsets_against_now(tmp_path: Path) -> None:
    factory = make_session_factory(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    now = datetime(2026, 1, 31, tzinfo=UTC)
    reset_and_seed(factory, now=now)
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Customer)) == 50
        assert s.scalar(select(func.count()).select_from(Order)) == 200
        assert s.scalar(select(func.count()).select_from(Subscription)) == 20
        order = s.get(Order, FIXTURE_DELIVERED_45_DAYS)
        assert order is not None
        assert order.delivered_at is not None
        assert as_utc(order.delivered_at) == now - timedelta(days=45)
        partial = s.get(Order, FIXTURE_PARTIAL_REFUND)
        assert partial is not None
        assert partial.refunded_cents == 4000


def test_reset_restores_seed_state(tmp_path: Path) -> None:
    factory = make_session_factory(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    reset_and_seed(factory)
    with factory() as s:
        order = s.get(Order, "NW-10003")
        assert order is not None
        order.status = "cancelled"
        s.commit()
    reset_and_seed(factory)
    with factory() as s:
        order = s.get(Order, "NW-10003")
        assert order is not None
        assert order.status == "processing"
