"""Request and response models for the mock store API. All money is integer cents."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class CustomerOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    created_at: datetime


class CustomerLookupOut(BaseModel):
    """More than one match is possible for a phone number (shared household phones)."""

    customers: list[CustomerOut]


class VerifyIn(BaseModel):
    order_number: str
    zip: str


class VerifyOut(BaseModel):
    verified: bool


class Address(BaseModel):
    line1: str = Field(min_length=1, max_length=128)
    city: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=2, max_length=2)
    zip: str = Field(pattern=r"^\d{5}$")


class LineItemOut(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


class RefundOut(BaseModel):
    amount_cents: int
    reason: str
    created_at: datetime


class ReturnOut(BaseModel):
    rma_number: str
    order_number: str
    status: str
    reason: str
    created_at: datetime


class OrderSummaryOut(BaseModel):
    order_number: str
    status: str
    placed_at: datetime
    total_cents: int


class OrderOut(OrderSummaryOut):
    customer_id: str
    shipped_at: datetime | None
    delivered_at: datetime | None
    tracking_number: str | None
    shipping_address: Address
    items: list[LineItemOut]
    refunds: list[RefundOut]
    returns: list[ReturnOut]
    refunded_cents: int
    refundable_cents: int
    return_eligible_until: datetime | None


class OrderListOut(BaseModel):
    orders: list[OrderSummaryOut]


class ReturnIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


class RefundIn(BaseModel):
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=256)


class SubscriptionOut(BaseModel):
    id: str
    customer_id: str
    sku: str
    name: str
    cadence_days: int
    price_cents: int
    status: Literal["active", "paused", "cancelled"]
    next_ship_at: datetime | None


class SubscriptionListOut(BaseModel):
    subscriptions: list[SubscriptionOut]


class LoyaltyOut(BaseModel):
    customer_id: str
    points_balance: int
    lifetime_points: int
    tier: Literal["bronze", "silver", "gold"]
