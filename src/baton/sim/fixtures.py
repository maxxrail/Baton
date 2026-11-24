"""Seed fixtures a scenario's persona can be: real customers and orders in the mock store.

A persona names a fixture (`persona.fixture: ava_outside_window`) and writes `<seeded>` where a
fact goes: `knows: {email: "<seeded>"}` becomes Ava's real email. The values are read from the
committed seed files (`data/seed/*.json`), so they can't drift from what the store loads.

Placeholders:
- `<seeded>`: the fixture's value for this key (`knows.zip` -> the fixture's `zip`);
- `<seeded:key>`: the fixture's value for another key (`<seeded:order_number_2>`);
- `<seeded:fixture.key>`: a value from another fixture
  (`<seeded:liam_partial_refund.order_number>`), for scenarios about someone else's account.
"""

import re
from dataclasses import dataclass
from functools import cache

from mock_commerce.seed import SeedData, read_seed_files


@dataclass(frozen=True)
class FixtureSpec:
    customer_id: str
    orders: tuple[str, ...] = ()
    subscription: str | None = None
    note: str = ""


# Ids are documented in src/mock_commerce/README.md (tricky cases) or picked from the seed.
FIXTURE_SPECS: dict[str, FixtureSpec] = {
    "ava_outside_window": FixtureSpec(
        "cus_0001", ("NW-10001",), note="blanket delivered 45 days ago: outside the window"
    ),
    "ava_inside_window": FixtureSpec(
        "cus_0001", ("NW-10002",), note="mugs + coffee delivered 10 days ago, $66.00"
    ),
    "ava_processing": FixtureSpec("cus_0001", ("NW-10003",), note="lamp placed yesterday"),
    "ava_two_orders": FixtureSpec(
        "cus_0001", ("NW-10003", "NW-10002"), note="one processing, one delivered 10 days ago"
    ),
    "liam_partial_refund": FixtureSpec(
        "cus_0002", ("NW-10004",), note="$214.00 order, $40.00 already refunded"
    ),
    "maya_shared_phone": FixtureSpec(
        "cus_0003", ("NW-10049", "NW-10110"), note="phone shared with cus_0004; two processing"
    ),
    "shipped_order": FixtureSpec("cus_0042", ("NW-10008",), note="in transit"),
    "cancelled_order": FixtureSpec("cus_0048", ("NW-10011",), note="cancelled and refunded"),
    "sub_active": FixtureSpec(
        "cus_0019", ("NW-10006",), subscription="sub_0001", note="active soap subscription"
    ),
    "sub_paused": FixtureSpec(
        "cus_0018", ("NW-10034",), subscription="sub_0002", note="paused coffee subscription"
    ),
    "loyalty_near_silver": FixtureSpec(
        "cus_0024", ("NW-10073",), note="loyalty account; also has an active subscription"
    ),
}

PLACEHOLDER_RE = re.compile(r"<seeded(?::([a-z0-9_]+(?:\.[a-z0-9_]+)?))?>")


class FixtureError(ValueError):
    pass


@cache
def _seed() -> SeedData:
    return read_seed_files()


@cache
def fixture_values(name: str) -> dict[str, str]:
    """Every fact a persona built on fixture `name` may know."""
    spec = FIXTURE_SPECS.get(name)
    if spec is None:
        raise FixtureError(f"unknown fixture {name!r} (known: {', '.join(sorted(FIXTURE_SPECS))})")
    seed = _seed()
    customer = next((c for c in seed.customers if c.id == spec.customer_id), None)
    if customer is None:
        raise FixtureError(f"fixture {name!r}: customer {spec.customer_id} is not in the seed")
    values = {
        "customer_id": customer.id,
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "name": f"{customer.first_name} {customer.last_name}",
        "email": customer.email,
        "phone": customer.phone,
        "zip": customer.zip,
    }
    orders = {o.order_number: o for o in seed.orders}
    for i, number in enumerate(spec.orders):
        order = orders.get(number)
        if order is None or order.customer_id != customer.id:
            raise FixtureError(f"fixture {name!r}: {number} is not {customer.id}'s order")
        suffix = "" if i == 0 else f"_{i + 1}"
        values[f"order_number{suffix}"] = number
        values[f"item{suffix}"] = order.items[0].name
        values[f"order_status{suffix}"] = order.status
    if spec.subscription:
        sub = next((s for s in seed.subscriptions if s.id == spec.subscription), None)
        if sub is None or sub.customer_id != customer.id:
            raise FixtureError(f"fixture {name!r}: {spec.subscription} is not {customer.id}'s")
        values["subscription_id"] = sub.id
        values["subscription"] = sub.name
    return values


def resolve(text: str, fixture: str | None, key: str | None = None) -> str:
    """Replace every placeholder in `text`. `key` is the field the text sits in (for a bare
    `<seeded>`). Raises `FixtureError` for anything that can't be resolved."""

    def sub(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref and "." in ref:
            other, field = ref.split(".", 1)
            values = fixture_values(other)
        else:
            if fixture is None:
                raise FixtureError(f"{match.group(0)} needs persona.fixture to be set")
            values = fixture_values(fixture)
            field = ref or key or ""
            if not field:
                raise FixtureError("a bare <seeded> only works as the value of a `knows` key")
        if field not in values:
            raise FixtureError(f"{match.group(0)}: no {field!r} (has: {', '.join(sorted(values))})")
        return values[field]

    return PLACEHOLDER_RE.sub(sub, text)
