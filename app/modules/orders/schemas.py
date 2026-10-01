"""Request and response shapes for checkout and orders. No queries here.

The checkout request carries two address ids and nothing else. It does not
carry prices, quantities, a cart id or a user id: every one of those is
resolved server-side, which is what makes it impossible to buy at a price
of the client's choosing or to check out somebody else's basket.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

OrderStatus = Literal["PENDING_PAYMENT", "CONFIRMED", "CANCELLED"]


class CheckoutRequest(BaseModel):
    shipping_address_id: uuid.UUID
    # Optional, defaulting to the shipping address. Most customers bill to
    # where they are shipping, and making them say so twice adds a required
    # field to the common path for the sake of the uncommon one. The
    # snapshot on the order is NOT NULL either way.
    billing_address_id: uuid.UUID | None = None


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variant_id: uuid.UUID
    product_id: uuid.UUID
    # Frozen at purchase. These are not read back from the catalogue, which
    # is why renaming or repricing a product leaves the order unchanged.
    product_name: str
    sku: str
    variant_option_summary: str | None
    quantity: int
    unit_price: Decimal
    currency: str
    discount_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_subtotal: Decimal
    line_total: Decimal


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    status: OrderStatus
    currency: str
    subtotal_amount: Decimal
    discount_total: Decimal
    tax_total: Decimal
    shipping_total: Decimal
    total_amount: Decimal
    shipping_address: dict[str, Any]
    billing_address: dict[str, Any]
    tax_rate_snapshot: dict[str, Any] | None
    shipping_rule_snapshot: dict[str, Any] | None
    # When this order's stock hold lapses. Reported so a client can warn a
    # customer rather than discovering it at payment.
    reservation_expires_at: datetime | None
    placed_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    items: list[OrderItemRead]


class OrderSummary(BaseModel):
    """A listing row. No lines and no addresses - a list is not a detail view."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    status: OrderStatus
    currency: str
    total_amount: Decimal
    placed_at: datetime
