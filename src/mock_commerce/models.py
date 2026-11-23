"""Tables for the Northwind Goods mock store. Money is stored as integer cents."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on read; treat stored datetimes as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)  # E.164, not unique on purpose
    zip: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list["Order"]] = relationship(
        back_populates="customer", order_by="Order.placed_at"
    )
    loyalty: Mapped["LoyaltyAccount | None"] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    order_number: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_number: Mapped[str | None] = mapped_column(String(32))
    ship_line1: Mapped[str] = mapped_column(String(128))
    ship_city: Mapped[str] = mapped_column(String(64))
    ship_state: Mapped[str] = mapped_column(String(2))
    ship_zip: Mapped[str] = mapped_column(String(10))
    total_cents: Mapped[int] = mapped_column(Integer)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["LineItem"]] = relationship(back_populates="order", order_by="LineItem.id")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="order", order_by="Refund.id")
    returns: Mapped[list["ReturnAuthorization"]] = relationship(
        back_populates="order", order_by="ReturnAuthorization.created_at"
    )

    @property
    def refunded_cents(self) -> int:
        return sum(r.amount_cents for r in self.refunds)

    @property
    def refundable_cents(self) -> int:
        return self.total_cents - self.refunded_cents


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(ForeignKey("orders.order_number"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(ForeignKey("orders.order_number"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="refunds")


class ReturnAuthorization(Base):
    """An RMA. `open` until the warehouse receives the items, then `received`."""

    __tablename__ = "returns"

    rma_number: Mapped[str] = mapped_column(String(16), primary_key=True)
    order_number: Mapped[str] = mapped_column(ForeignKey("orders.order_number"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="returns")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    cadence_days: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    next_ship_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoyaltyAccount(Base):
    __tablename__ = "loyalty_accounts"

    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    points_balance: Mapped[int] = mapped_column(Integer)
    lifetime_points: Mapped[int] = mapped_column(Integer)

    customer: Mapped[Customer] = relationship(back_populates="loyalty")
