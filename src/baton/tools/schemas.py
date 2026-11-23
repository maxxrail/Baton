"""Typed inputs and outputs for the Northwind tools.

These are Baton's own models, not imports from `mock_commerce`: the mock stands in for an
external system, and Baton should only depend on the shape of its HTTP API. All money is integer
cents, matching the store API.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

OrderStatus = Literal["processing", "shipped", "delivered", "cancelled"]
SubscriptionStatus = Literal["active", "paused", "cancelled"]

# ---------------------------------------------------------------- inputs


class LookupCustomerIn(BaseModel):
    """Find an account by email or phone (exactly one)."""

    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _exactly_one(self) -> "LookupCustomerIn":
        if (self.email is None) == (self.phone is None):
            raise ValueError("provide exactly one of email or phone")
        return self


class VerifyCustomerIn(BaseModel):
    customer_id: str
    order_number: str = Field(description="Any order on the account, e.g. NW-10042")
    zip: str = Field(pattern=r"^\d{5}$", description="Shipping ZIP code of that order")


class CustomerIdIn(BaseModel):
    customer_id: str


class OrderNumberIn(BaseModel):
    order_number: str = Field(description="Order number, e.g. NW-10042")


class AddressIn(BaseModel):
    line1: str = Field(min_length=1, max_length=128)
    city: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=2, max_length=2, description="Two-letter state code")
    zip: str = Field(pattern=r"^\d{5}$")


class ChangeAddressIn(BaseModel):
    order_number: str
    address: AddressIn


class CreateReturnIn(BaseModel):
    order_number: str
    reason: str = Field(min_length=1, max_length=256)


class CreateRefundIn(BaseModel):
    order_number: str
    amount_cents: int = Field(gt=0, description="Refund amount in cents")
    reason: str = Field(min_length=1, max_length=256)


class SubscriptionIdIn(BaseModel):
    subscription_id: str


# ---------------------------------------------------------------- outputs


class Customer(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str


class CustomerLookupOut(BaseModel):
    """A phone number can match more than one account (shared household phones)."""

    customers: list[Customer]


class VerifyCustomerOut(BaseModel):
    verified: bool


class Address(BaseModel):
    line1: str
    city: str
    state: str
    zip: str


class LineItem(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


class Refund(BaseModel):
    amount_cents: int
    reason: str
    created_at: datetime


class ReturnAuthorization(BaseModel):
    rma_number: str
    order_number: str
    status: str
    reason: str
    created_at: datetime


class OrderSummary(BaseModel):
    order_number: str
    status: OrderStatus
    placed_at: datetime
    total_cents: int


class OrderListOut(BaseModel):
    orders: list[OrderSummary]


class Order(OrderSummary):
    customer_id: str
    shipped_at: datetime | None
    delivered_at: datetime | None
    tracking_number: str | None
    shipping_address: Address
    items: list[LineItem]
    refunds: list[Refund]
    returns: list[ReturnAuthorization]
    refunded_cents: int
    refundable_cents: int
    return_eligible_until: datetime | None


class Subscription(BaseModel):
    id: str
    customer_id: str
    sku: str
    name: str
    cadence_days: int
    price_cents: int
    status: SubscriptionStatus
    next_ship_at: datetime | None


class SubscriptionListOut(BaseModel):
    subscriptions: list[Subscription]


class Loyalty(BaseModel):
    customer_id: str
    points_balance: int
    lifetime_points: int
    tier: Literal["bronze", "silver", "gold"]
