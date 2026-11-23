"""Northwind Goods mock commerce API.

A stand-in for a brand's real store backend. Baton's tools call it over HTTP. Business rules are
enforced here, server-side, the way a real backend would: a rule violation is a 4xx with a
machine-readable `error.code` (see `ErrorCode`), never a silent success.

Run alone with `uv run uvicorn mock_commerce.app:app --port 8100`, or via `scripts/dev.py`.
"""

import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mock_commerce.db import make_session_factory
from mock_commerce.models import (
    Base,
    Customer,
    LoyaltyAccount,
    Order,
    Refund,
    ReturnAuthorization,
    Subscription,
    as_utc,
)
from mock_commerce.schemas import (
    Address,
    CustomerLookupOut,
    CustomerOut,
    ErrorResponse,
    LineItemOut,
    LoyaltyOut,
    OrderListOut,
    OrderOut,
    OrderSummaryOut,
    RefundIn,
    RefundOut,
    ReturnIn,
    ReturnOut,
    SubscriptionListOut,
    SubscriptionOut,
    VerifyIn,
    VerifyOut,
)
from mock_commerce.seed import RETURN_WINDOW_DAYS, reset_and_seed

Clock = Callable[[], datetime]


class ErrorCode(StrEnum):
    INVALID_LOOKUP = "INVALID_LOOKUP"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"
    ADDRESS_CHANGE_NOT_ALLOWED = "ADDRESS_CHANGE_NOT_ALLOWED"
    ORDER_NOT_RETURNABLE = "ORDER_NOT_RETURNABLE"
    RETURN_WINDOW_EXPIRED = "RETURN_WINDOW_EXPIRED"
    RETURN_ALREADY_EXISTS = "RETURN_ALREADY_EXISTS"
    REFUND_EXCEEDS_BALANCE = "REFUND_EXCEEDS_BALANCE"
    SUBSCRIPTION_NOT_ACTIVE = "SUBSCRIPTION_NOT_ACTIVE"
    SUBSCRIPTION_NOT_PAUSED = "SUBSCRIPTION_NOT_PAUSED"
    SUBSCRIPTION_ALREADY_CANCELLED = "SUBSCRIPTION_ALREADY_CANCELLED"


class CommerceError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_found(code: ErrorCode, what: str) -> CommerceError:
    return CommerceError(404, code, f"{what} not found")


def _conflict(code: ErrorCode, message: str) -> CommerceError:
    return CommerceError(409, code, message)


# --------------------------------------------------------------------------- dependencies


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_now(request: Request) -> datetime:
    clock: Clock = request.app.state.clock
    return clock()


SessionDep = Annotated[Session, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]


def _customer(session: Session, customer_id: str) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise _not_found(ErrorCode.CUSTOMER_NOT_FOUND, "customer")
    return customer


def _order(session: Session, order_number: str, *, for_update: bool = False) -> Order:
    """Load an order. Mutating endpoints pass `for_update=True` so concurrent writes to the
    same order serialise (SELECT ... FOR UPDATE on Postgres; a no-op on SQLite, which already
    serialises writers). Without it two refunds could both pass the balance check."""
    order = session.get(Order, order_number.strip().upper(), with_for_update=for_update)
    if order is None:
        raise _not_found(ErrorCode.ORDER_NOT_FOUND, "order")
    return order


def _subscription(session: Session, subscription_id: str) -> Subscription:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        raise _not_found(ErrorCode.SUBSCRIPTION_NOT_FOUND, "subscription")
    return sub


# --------------------------------------------------------------------------- serialisers


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def _return_deadline(order: Order) -> datetime | None:
    if order.delivered_at is None:
        return None
    return as_utc(order.delivered_at) + timedelta(days=RETURN_WINDOW_DAYS)


def _customer_out(c: Customer) -> CustomerOut:
    return CustomerOut(
        id=c.id,
        first_name=c.first_name,
        last_name=c.last_name,
        email=c.email,
        phone=c.phone,
        created_at=as_utc(c.created_at),
    )


def _summary_out(o: Order) -> OrderSummaryOut:
    return OrderSummaryOut(
        order_number=o.order_number,
        status=o.status,
        placed_at=as_utc(o.placed_at),
        total_cents=o.total_cents,
    )


def _return_out(r: ReturnAuthorization) -> ReturnOut:
    return ReturnOut(
        rma_number=r.rma_number,
        order_number=r.order_number,
        status=r.status,
        reason=r.reason,
        created_at=as_utc(r.created_at),
    )


def _order_out(o: Order) -> OrderOut:
    return OrderOut(
        order_number=o.order_number,
        customer_id=o.customer_id,
        status=o.status,
        placed_at=as_utc(o.placed_at),
        shipped_at=as_utc(o.shipped_at) if o.shipped_at else None,
        delivered_at=as_utc(o.delivered_at) if o.delivered_at else None,
        tracking_number=o.tracking_number,
        shipping_address=Address(
            line1=o.ship_line1, city=o.ship_city, state=o.ship_state, zip=o.ship_zip
        ),
        total_cents=o.total_cents,
        items=[
            LineItemOut(
                sku=i.sku, name=i.name, quantity=i.quantity, unit_price_cents=i.unit_price_cents
            )
            for i in o.items
        ],
        refunds=[
            RefundOut(amount_cents=r.amount_cents, reason=r.reason, created_at=as_utc(r.created_at))
            for r in o.refunds
        ],
        returns=[_return_out(r) for r in o.returns],
        refunded_cents=o.refunded_cents,
        refundable_cents=o.refundable_cents,
        return_eligible_until=_return_deadline(o) if o.status == "delivered" else None,
    )


def _subscription_out(s: Subscription) -> SubscriptionOut:
    return SubscriptionOut.model_validate(
        {
            "id": s.id,
            "customer_id": s.customer_id,
            "sku": s.sku,
            "name": s.name,
            "cadence_days": s.cadence_days,
            "price_cents": s.price_cents,
            "status": s.status,
            "next_ship_at": as_utc(s.next_ship_at) if s.next_ship_at else None,
        }
    )


def loyalty_tier(lifetime_points: int) -> str:
    if lifetime_points >= 2500:
        return "gold"
    if lifetime_points >= 1000:
        return "silver"
    return "bronze"


# --------------------------------------------------------------------------- app


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
    seed_on_startup: bool = True,
) -> FastAPI:
    """Build the app. Tests inject an in-memory/temporary DB and a fixed clock."""
    factory = session_factory or make_session_factory()
    now: Clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with factory() as session:
            Base.metadata.create_all(session.get_bind())
            empty = session.scalar(select(func.count()).select_from(Customer)) == 0
        if seed_on_startup and empty:
            reset_and_seed(factory, now=now())
        yield

    app = FastAPI(
        title="Northwind Goods — mock commerce API",
        version="0.1.0",
        lifespan=lifespan,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    app.state.session_factory = factory
    app.state.clock = now

    @app.exception_handler(CommerceError)
    async def _commerce_error(_request: Request, exc: CommerceError) -> JSONResponse:
        body = ErrorResponse.model_validate({"error": {"code": exc.code, "message": exc.message}})
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- customers

    @app.get("/customers/lookup")
    def lookup_customer(
        session: SessionDep,
        email: Annotated[str | None, Query()] = None,
        phone: Annotated[str | None, Query()] = None,
    ) -> CustomerLookupOut:
        if (email is None) == (phone is None):
            raise CommerceError(
                422, ErrorCode.INVALID_LOOKUP, "provide exactly one of email or phone"
            )
        if email is not None:
            stmt = select(Customer).where(func.lower(Customer.email) == email.strip().lower())
        else:
            assert phone is not None
            stmt = select(Customer).where(Customer.phone == normalize_phone(phone))
        matches = session.scalars(stmt.order_by(Customer.id)).all()
        if not matches:
            raise _not_found(ErrorCode.CUSTOMER_NOT_FOUND, "customer")
        return CustomerLookupOut(customers=[_customer_out(c) for c in matches])

    @app.post("/customers/{customer_id}/verify")
    def verify_customer(customer_id: str, body: VerifyIn, session: SessionDep) -> VerifyOut:
        """True only if the order belongs to this customer and ships to `zip`.

        A wrong order number and a wrong zip look the same, so the response leaks nothing.
        """
        customer = _customer(session, customer_id)
        order = session.get(Order, body.order_number.strip().upper())
        ok = (
            order is not None
            and order.customer_id == customer.id
            and order.ship_zip == body.zip.strip()
        )
        return VerifyOut(verified=ok)

    @app.get("/customers/{customer_id}/orders")
    def list_orders(customer_id: str, session: SessionDep) -> OrderListOut:
        customer = _customer(session, customer_id)
        return OrderListOut(orders=[_summary_out(o) for o in reversed(customer.orders)])

    @app.get("/customers/{customer_id}/loyalty")
    def get_loyalty(customer_id: str, session: SessionDep) -> LoyaltyOut:
        _customer(session, customer_id)
        account = session.get(LoyaltyAccount, customer_id)
        balance, lifetime = (account.points_balance, account.lifetime_points) if account else (0, 0)
        return LoyaltyOut.model_validate(
            {
                "customer_id": customer_id,
                "points_balance": balance,
                "lifetime_points": lifetime,
                "tier": loyalty_tier(lifetime),
            }
        )

    # ---- orders

    @app.get("/orders/{order_number}")
    def get_order(order_number: str, session: SessionDep) -> OrderOut:
        return _order_out(_order(session, order_number))

    @app.post("/orders/{order_number}/cancel")
    def cancel_order(order_number: str, session: SessionDep, now: NowDep) -> OrderOut:
        order = _order(session, order_number, for_update=True)
        if order.status != "processing":
            raise _conflict(
                ErrorCode.ORDER_NOT_CANCELLABLE,
                f"only processing orders can be cancelled; this order is {order.status}",
            )
        order.status = "cancelled"
        if order.refundable_cents > 0:  # cancelling refunds whatever was charged
            order.refunds.append(
                Refund(
                    amount_cents=order.refundable_cents, reason="Order cancelled", created_at=now
                )
            )
        session.flush()
        return _order_out(order)

    @app.post("/orders/{order_number}/address")
    def change_address(order_number: str, body: Address, session: SessionDep) -> OrderOut:
        order = _order(session, order_number, for_update=True)
        if order.status != "processing":
            raise _conflict(
                ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED,
                f"address can only change while processing; this order is {order.status}",
            )
        order.ship_line1, order.ship_city = body.line1, body.city
        order.ship_state, order.ship_zip = body.state.upper(), body.zip
        session.flush()
        return _order_out(order)

    @app.post("/orders/{order_number}/return", status_code=201)
    def create_return(
        order_number: str, body: ReturnIn, session: SessionDep, now: NowDep
    ) -> ReturnOut:
        order = _order(session, order_number, for_update=True)
        if order.status != "delivered":
            raise _conflict(
                ErrorCode.ORDER_NOT_RETURNABLE,
                f"only delivered orders can be returned; this order is {order.status}",
            )
        deadline = _return_deadline(order)
        assert deadline is not None  # delivered orders always have delivered_at
        if as_utc(now) > deadline:
            raise _conflict(
                ErrorCode.RETURN_WINDOW_EXPIRED,
                f"the {RETURN_WINDOW_DAYS}-day return window closed on {deadline.date()}",
            )
        if any(r.status == "open" for r in order.returns):
            raise _conflict(ErrorCode.RETURN_ALREADY_EXISTS, "an open return already exists")
        rma = ReturnAuthorization(
            rma_number=f"RMA-{order.order_number.removeprefix('NW-')}-{len(order.returns) + 1}",
            order_number=order.order_number,
            status="open",
            reason=body.reason,
            created_at=now,
        )
        session.add(rma)
        session.flush()
        return _return_out(rma)

    @app.post("/orders/{order_number}/refund", status_code=201)
    def refund_order(
        order_number: str, body: RefundIn, session: SessionDep, now: NowDep
    ) -> OrderOut:
        order = _order(session, order_number, for_update=True)
        if body.amount_cents > order.refundable_cents:
            raise _conflict(
                ErrorCode.REFUND_EXCEEDS_BALANCE,
                f"requested {body.amount_cents} cents but only {order.refundable_cents} "
                "cents remain refundable",
            )
        order.refunds.append(
            Refund(amount_cents=body.amount_cents, reason=body.reason, created_at=now)
        )
        session.flush()
        return _order_out(order)

    # ---- subscriptions

    @app.get("/subscriptions/{customer_id}")
    def list_subscriptions(customer_id: str, session: SessionDep) -> SubscriptionListOut:
        _customer(session, customer_id)
        subs = session.scalars(
            select(Subscription).where(Subscription.customer_id == customer_id)
        ).all()
        return SubscriptionListOut(subscriptions=[_subscription_out(s) for s in subs])

    @app.post("/subscriptions/{subscription_id}/pause")
    def pause_subscription(subscription_id: str, session: SessionDep) -> SubscriptionOut:
        sub = _subscription(session, subscription_id)
        if sub.status != "active":
            raise _conflict(ErrorCode.SUBSCRIPTION_NOT_ACTIVE, f"subscription is {sub.status}")
        sub.status, sub.next_ship_at = "paused", None
        session.flush()
        return _subscription_out(sub)

    @app.post("/subscriptions/{subscription_id}/resume")
    def resume_subscription(
        subscription_id: str, session: SessionDep, now: NowDep
    ) -> SubscriptionOut:
        sub = _subscription(session, subscription_id)
        if sub.status != "paused":
            raise _conflict(ErrorCode.SUBSCRIPTION_NOT_PAUSED, f"subscription is {sub.status}")
        sub.status, sub.next_ship_at = "active", now + timedelta(days=sub.cadence_days)
        session.flush()
        return _subscription_out(sub)

    @app.post("/subscriptions/{subscription_id}/cancel")
    def cancel_subscription(subscription_id: str, session: SessionDep) -> SubscriptionOut:
        sub = _subscription(session, subscription_id)
        if sub.status == "cancelled":
            raise _conflict(ErrorCode.SUBSCRIPTION_ALREADY_CANCELLED, "already cancelled")
        sub.status, sub.next_ship_at = "cancelled", None
        session.flush()
        return _subscription_out(sub)

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn mock_commerce.app:app` builds the app lazily, so importing this module in tests
    # doesn't create mock_commerce.db as a side effect.
    if name == "app":
        return create_app()
    raise AttributeError(name)
