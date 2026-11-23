"""Deterministic seed data for Northwind Goods.

Two jobs:
1. `generate_seed_data()` builds the dataset from a fixed random seed. Run
   `uv run python -m mock_commerce.seed --generate` to rewrite `data/seed/*.json`.
2. `load_seed()` reads those JSON files into the mock store's database.

Times are stored as day offsets (`delivered_days_ago: 45`) and resolved against "now" when
loaded, so "delivered 45 days ago" is still true whenever the store starts.

The first few customers are hand-written fixtures for tricky cases; see `FIXTURES` below and
`src/mock_commerce/README.md`. Everything after them is random but reproducible.
"""

import argparse
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from mock_commerce.models import (
    Base,
    Customer,
    LineItem,
    LoyaltyAccount,
    Order,
    Refund,
    ReturnAuthorization,
    Subscription,
)

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"
RANDOM_SEED = 20260101
N_CUSTOMERS = 50
N_ORDERS = 200
N_SUBSCRIPTIONS = 20
RETURN_WINDOW_DAYS = 30

OrderStatus = Literal["processing", "shipped", "delivered", "returned", "cancelled"]
SubscriptionStatus = Literal["active", "paused", "cancelled"]


# --------------------------------------------------------------------------- seed file models


class SeedCustomer(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    zip: str
    created_days_ago: int


class SeedLineItem(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


class SeedRefund(BaseModel):
    amount_cents: int
    reason: str
    days_ago: int


class SeedReturn(BaseModel):
    rma_number: str
    status: Literal["open", "received"]
    reason: str
    days_ago: int


class SeedOrder(BaseModel):
    order_number: str
    customer_id: str
    status: OrderStatus
    placed_days_ago: int
    shipped_days_ago: int | None = None
    delivered_days_ago: int | None = None
    tracking_number: str | None = None
    ship_line1: str
    ship_city: str
    ship_state: str
    ship_zip: str
    items: list[SeedLineItem]
    refunds: list[SeedRefund] = []
    returns: list[SeedReturn] = []

    @property
    def total_cents(self) -> int:
        return sum(i.quantity * i.unit_price_cents for i in self.items)


class SeedSubscription(BaseModel):
    id: str
    customer_id: str
    sku: str
    name: str
    cadence_days: int
    price_cents: int
    status: SubscriptionStatus
    next_ship_in_days: int | None = None


class SeedLoyalty(BaseModel):
    customer_id: str
    points_balance: int
    lifetime_points: int


class SeedData(BaseModel):
    customers: list[SeedCustomer]
    orders: list[SeedOrder]
    subscriptions: list[SeedSubscription]
    loyalty: list[SeedLoyalty]


# --------------------------------------------------------------------------- catalog + fixtures

CATALOG: list[tuple[str, str, int]] = [
    ("NW-MUG-01", "Stoneware Mug", 2400),
    ("NW-TOWEL-02", "Linen Tea Towel (set of 2)", 2800),
    ("NW-BLANKET-03", "Merino Throw Blanket", 14900),
    ("NW-CANDLE-04", "Cedar & Sage Candle", 3600),
    ("NW-COFFEE-05", "Northwind House Coffee, 12oz", 1800),
    ("NW-TEA-06", "Smoky Earl Grey Tea, 50 bags", 1400),
    ("NW-BOARD-07", "Walnut Cutting Board", 8900),
    ("NW-KNIFE-08", "Chef's Knife, 8in", 12500),
    ("NW-PILLOW-09", "Wool Cushion Cover", 5200),
    ("NW-BOTTLE-10", "Insulated Water Bottle", 3200),
    ("NW-SOAP-11", "Olive Oil Bar Soap (3-pack)", 1600),
    ("NW-LAMP-12", "Brass Reading Lamp", 18900),
    ("NW-SOCKS-13", "Alpaca Socks", 2200),
    ("NW-PLANTER-14", "Terracotta Planter", 3900),
    ("NW-JOURNAL-15", "Linen-Bound Journal", 2600),
]
SUBSCRIBABLE: list[tuple[str, str, int]] = [
    ("NW-COFFEE-05", "Northwind House Coffee, 12oz", 1700),
    ("NW-TEA-06", "Smoky Earl Grey Tea, 50 bags", 1300),
    ("NW-SOAP-11", "Olive Oil Bar Soap (3-pack)", 1500),
    ("NW-CANDLE-04", "Cedar & Sage Candle", 3200),
]
FIRST_NAMES = [
    "Ava", "Liam", "Maya", "Noah", "Zoe", "Ethan", "Priya", "Lucas", "Amara", "Mateo",
    "Hana", "Owen", "Sofia", "Kai", "Leila", "Jonah", "Nora", "Ravi", "Elena", "Theo",
]  # fmt: skip
LAST_NAMES = [
    "Thompson", "Okafor", "Nguyen", "Garcia", "Patel", "Kowalski", "Haddad", "Silva",
    "Andersen", "Moreau", "Tanaka", "Byrne", "Rossi", "Mensah", "Novak", "Ortiz",
]  # fmt: skip
CITIES: list[tuple[str, str, str]] = [
    ("Seattle", "WA", "98101"),
    ("Portland", "OR", "97205"),
    ("Austin", "TX", "78701"),
    ("Denver", "CO", "80202"),
    ("Chicago", "IL", "60601"),
    ("Brooklyn", "NY", "11201"),
    ("Atlanta", "GA", "30303"),
    ("Minneapolis", "MN", "55401"),
    ("Boston", "MA", "02108"),
    ("Oakland", "CA", "94612"),
]
AREA_CODES = ["206", "503", "512", "303", "312", "718", "404", "612", "617", "510"]
STREETS = ["Maple St", "Harbor Ave", "Pine Rd", "Elm Ct", "Juniper Way", "Cedar Ln", "Birch Blvd"]

# Hand-written tricky cases. IDs are stable; tests and simulations rely on them.
FIXTURE_DELIVERED_45_DAYS = "NW-10001"  # outside the 30-day return window
FIXTURE_DELIVERED_10_DAYS = "NW-10002"  # inside the window
FIXTURE_PROCESSING = "NW-10003"  # can be cancelled / readdressed
FIXTURE_PARTIAL_REFUND = "NW-10004"  # already partially refunded
FIXTURE_SHARED_PHONE = "+15035550199"  # two customer accounts share this number
FIXTURE_SHARED_PHONE_CUSTOMERS = ("cus_0003", "cus_0004")

FIXTURES = SeedData(
    customers=[
        SeedCustomer(
            id="cus_0001",
            first_name="Ava",
            last_name="Thompson",
            email="ava.thompson@example.com",
            phone="+12065550101",
            zip="98101",
            created_days_ago=400,
        ),
        SeedCustomer(
            id="cus_0002",
            first_name="Liam",
            last_name="Okafor",
            email="liam.okafor@example.com",
            phone="+15125550102",
            zip="78701",
            created_days_ago=300,
        ),
        SeedCustomer(
            id="cus_0003",
            first_name="Maya",
            last_name="Nguyen",
            email="maya.nguyen@example.com",
            phone=FIXTURE_SHARED_PHONE,
            zip="97205",
            created_days_ago=700,
        ),
        SeedCustomer(
            id="cus_0004",
            first_name="Maya",
            last_name="Nguyen",
            email="maya.n.work@example.com",
            phone=FIXTURE_SHARED_PHONE,
            zip="97205",
            created_days_ago=90,
        ),
    ],
    orders=[
        SeedOrder(
            order_number=FIXTURE_DELIVERED_45_DAYS,
            customer_id="cus_0001",
            status="delivered",
            placed_days_ago=52,
            shipped_days_ago=50,
            delivered_days_ago=45,
            tracking_number="1ZNW000000000001",
            ship_line1="12 Harbor Ave",
            ship_city="Seattle",
            ship_state="WA",
            ship_zip="98101",
            items=[
                SeedLineItem(
                    sku="NW-BLANKET-03",
                    name="Merino Throw Blanket",
                    quantity=1,
                    unit_price_cents=14900,
                )
            ],
        ),
        SeedOrder(
            order_number=FIXTURE_DELIVERED_10_DAYS,
            customer_id="cus_0001",
            status="delivered",
            placed_days_ago=16,
            shipped_days_ago=14,
            delivered_days_ago=10,
            tracking_number="1ZNW000000000002",
            ship_line1="12 Harbor Ave",
            ship_city="Seattle",
            ship_state="WA",
            ship_zip="98101",
            items=[
                SeedLineItem(
                    sku="NW-MUG-01", name="Stoneware Mug", quantity=2, unit_price_cents=2400
                ),
                SeedLineItem(
                    sku="NW-COFFEE-05",
                    name="Northwind House Coffee, 12oz",
                    quantity=1,
                    unit_price_cents=1800,
                ),
            ],
        ),
        SeedOrder(
            order_number=FIXTURE_PROCESSING,
            customer_id="cus_0001",
            status="processing",
            placed_days_ago=1,
            ship_line1="12 Harbor Ave",
            ship_city="Seattle",
            ship_state="WA",
            ship_zip="98101",
            items=[
                SeedLineItem(
                    sku="NW-LAMP-12", name="Brass Reading Lamp", quantity=1, unit_price_cents=18900
                )
            ],
        ),
        SeedOrder(
            order_number=FIXTURE_PARTIAL_REFUND,
            customer_id="cus_0002",
            status="delivered",
            placed_days_ago=20,
            shipped_days_ago=18,
            delivered_days_ago=12,
            tracking_number="1ZNW000000000004",
            ship_line1="400 Congress Ave",
            ship_city="Austin",
            ship_state="TX",
            ship_zip="78701",
            items=[
                SeedLineItem(
                    sku="NW-KNIFE-08", name="Chef's Knife, 8in", quantity=1, unit_price_cents=12500
                ),
                SeedLineItem(
                    sku="NW-BOARD-07",
                    name="Walnut Cutting Board",
                    quantity=1,
                    unit_price_cents=8900,
                ),
            ],
            refunds=[
                SeedRefund(amount_cents=4000, reason="Cutting board arrived scratched", days_ago=8)
            ],
        ),
    ],
    subscriptions=[],
    loyalty=[],
)


# --------------------------------------------------------------------------- generator


def _status_timeline(rng: random.Random, status: OrderStatus) -> tuple[int, int | None, int | None]:
    """(placed, shipped, delivered) days ago, consistent with the status."""
    if status == "processing":
        return rng.randint(0, 3), None, None
    if status == "cancelled":
        return rng.randint(2, 120), None, None
    if status == "shipped":
        placed = rng.randint(2, 8)
        return placed, placed - 1, None
    # delivered or returned
    placed = rng.randint(6, 180)
    shipped = placed - rng.randint(1, 3)
    delivered = max(shipped - rng.randint(2, 6), 1)
    return placed, shipped, delivered


def generate_seed_data(seed: int = RANDOM_SEED) -> SeedData:
    rng = random.Random(seed)
    data = FIXTURES.model_copy(deep=True)

    used_emails = {c.email for c in data.customers}
    for n in range(len(data.customers) + 1, N_CUSTOMERS + 1):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        email = f"{first}.{last}{n}@example.com".lower()
        assert email not in used_emails
        used_emails.add(email)
        city_idx = rng.randrange(len(CITIES))
        data.customers.append(
            SeedCustomer(
                id=f"cus_{n:04d}",
                first_name=first,
                last_name=last,
                email=email,
                # 555-01xx numbers are reserved for fiction; n keeps each one unique.
                phone=f"+1{AREA_CODES[city_idx]}55501{n:02d}",
                zip=CITIES[city_idx][2],
                created_days_ago=rng.randint(30, 900),
            )
        )

    statuses: list[OrderStatus] = ["processing", "shipped", "delivered", "returned", "cancelled"]
    weights = [12, 18, 50, 10, 10]
    customers_by_id = {c.id: c for c in data.customers}
    random_customers = [c for c in data.customers if c.id not in {"cus_0001", "cus_0002"}]
    for n in range(len(data.orders) + 1, N_ORDERS + 1):
        customer = rng.choice(random_customers)
        status = rng.choices(statuses, weights)[0]
        placed, shipped, delivered = _status_timeline(rng, status)
        city = next(c for c in CITIES if c[2] == customers_by_id[customer.id].zip)
        items = [
            SeedLineItem(sku=sku, name=name, quantity=rng.randint(1, 2), unit_price_cents=price)
            for sku, name, price in rng.sample(CATALOG, rng.randint(1, 3))
        ]
        order = SeedOrder(
            order_number=f"NW-{10000 + n}",
            customer_id=customer.id,
            status=status,
            placed_days_ago=placed,
            shipped_days_ago=shipped,
            delivered_days_ago=delivered,
            tracking_number=f"1ZNW{n:012d}" if shipped is not None else None,
            ship_line1=f"{rng.randint(10, 9999)} {rng.choice(STREETS)}",
            ship_city=city[0],
            ship_state=city[1],
            ship_zip=city[2],
            items=items,
        )
        if status == "cancelled":
            order.refunds.append(
                SeedRefund(
                    amount_cents=order.total_cents, reason="Order cancelled", days_ago=placed
                )
            )
        if status == "returned":
            assert delivered is not None
            returned_ago = max(delivered - rng.randint(2, 10), 0)
            order.returns.append(
                SeedReturn(
                    rma_number=f"RMA-{n:05d}",
                    status="received",
                    reason=rng.choice(["Wrong size", "Changed my mind", "Arrived damaged"]),
                    days_ago=returned_ago,
                )
            )
            order.refunds.append(
                SeedRefund(
                    amount_cents=order.total_cents, reason="Return received", days_ago=returned_ago
                )
            )
        data.orders.append(order)

    sub_customers = rng.sample(data.customers, N_SUBSCRIPTIONS)
    sub_statuses: list[SubscriptionStatus] = ["active", "paused", "cancelled"]
    for n, customer in enumerate(sub_customers, start=1):
        sku, name, price = rng.choice(SUBSCRIBABLE)
        sub_status = rng.choices(sub_statuses, [14, 4, 2])[0]
        data.subscriptions.append(
            SeedSubscription(
                id=f"sub_{n:04d}",
                customer_id=customer.id,
                sku=sku,
                name=name,
                cadence_days=rng.choice([14, 30, 60]),
                price_cents=price,
                status=sub_status,
                next_ship_in_days=rng.randint(1, 30) if sub_status == "active" else None,
            )
        )

    for customer in data.customers:
        lifetime = rng.randint(0, 60) * 50
        data.loyalty.append(
            SeedLoyalty(
                customer_id=customer.id,
                points_balance=rng.randint(0, lifetime // 50) * 50 if lifetime else 0,
                lifetime_points=lifetime,
            )
        )
    return data


# --------------------------------------------------------------------------- files

_FILES = ("customers", "orders", "subscriptions", "loyalty")


def write_seed_files(data: SeedData, seed_dir: Path = SEED_DIR) -> None:
    seed_dir.mkdir(parents=True, exist_ok=True)
    dumped = data.model_dump(mode="json", exclude_none=True)
    for name in _FILES:
        text = json.dumps(dumped[name], indent=2, ensure_ascii=False) + "\n"
        (seed_dir / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")


def read_seed_files(seed_dir: Path = SEED_DIR) -> SeedData:
    raw = {n: json.loads((seed_dir / f"{n}.json").read_text(encoding="utf-8")) for n in _FILES}
    return SeedData.model_validate(raw)


# --------------------------------------------------------------------------- loader


def load_seed(session: Session, data: SeedData, now: datetime | None = None) -> None:
    """Insert `data` into an empty store. Day offsets are resolved against `now`."""
    now = now or datetime.now(UTC)

    def ago(days: int | None) -> datetime | None:
        return None if days is None else now - timedelta(days=days)

    def ago_req(days: int) -> datetime:
        return now - timedelta(days=days)

    for c in data.customers:
        session.add(
            Customer(
                id=c.id,
                first_name=c.first_name,
                last_name=c.last_name,
                email=c.email,
                phone=c.phone,
                zip=c.zip,
                created_at=ago_req(c.created_days_ago),
            )
        )
    for o in data.orders:
        session.add(
            Order(
                order_number=o.order_number,
                customer_id=o.customer_id,
                status=o.status,
                placed_at=ago_req(o.placed_days_ago),
                shipped_at=ago(o.shipped_days_ago),
                delivered_at=ago(o.delivered_days_ago),
                tracking_number=o.tracking_number,
                ship_line1=o.ship_line1,
                ship_city=o.ship_city,
                ship_state=o.ship_state,
                ship_zip=o.ship_zip,
                total_cents=o.total_cents,
                items=[LineItem(**i.model_dump()) for i in o.items],
                refunds=[
                    Refund(
                        amount_cents=r.amount_cents, reason=r.reason, created_at=ago_req(r.days_ago)
                    )
                    for r in o.refunds
                ],
                returns=[
                    ReturnAuthorization(
                        rma_number=r.rma_number,
                        status=r.status,
                        reason=r.reason,
                        created_at=ago_req(r.days_ago),
                    )
                    for r in o.returns
                ],
            )
        )
    for s in data.subscriptions:
        session.add(
            Subscription(
                id=s.id,
                customer_id=s.customer_id,
                sku=s.sku,
                name=s.name,
                cadence_days=s.cadence_days,
                price_cents=s.price_cents,
                status=s.status,
                next_ship_at=None
                if s.next_ship_in_days is None
                else now + timedelta(days=s.next_ship_in_days),
            )
        )
    for loyalty in data.loyalty:
        session.add(LoyaltyAccount(**loyalty.model_dump()))
    session.flush()


def reset_and_seed(
    session_factory: Callable[[], Session],
    data: SeedData | None = None,
    now: datetime | None = None,
) -> None:
    """Drop and recreate every table, then load the seed. Used by the app and the dev runner."""
    with session_factory() as session:
        bind = session.get_bind()
        Base.metadata.drop_all(bind)
        Base.metadata.create_all(bind)
        load_seed(session, data or read_seed_files(), now)
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Northwind Goods seed data")
    parser.add_argument("--generate", action="store_true", help="rewrite data/seed/*.json")
    parser.add_argument("--db", help="database URL to reset and load (default: settings)")
    args = parser.parse_args()

    if args.generate:
        write_seed_files(generate_seed_data())
        print(f"wrote seed files to {SEED_DIR}")
        return

    from mock_commerce.db import make_session_factory

    reset_and_seed(make_session_factory(args.db))
    print("mock commerce database reset and seeded")


if __name__ == "__main__":
    main()
