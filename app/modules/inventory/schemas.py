"""Request and response shapes for inventory. No queries here."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Bucket = Literal["ON_HAND", "DAMAGED", "QUARANTINED"]
ReservationStatus = Literal["ACTIVE", "CONVERTED", "EXPIRED", "RELEASED"]
TransactionType = Literal[
    "PURCHASE", "RESTOCK", "RETURN", "ADJUSTMENT", "DISPOSAL", "TRANSFER"
]
# What an operator may write by hand. PURCHASE is absent because only a
# reservation conversion records a sale; RETURN because Phase 7 owns returns.
AdjustableType = Literal["ADJUSTMENT", "RESTOCK", "DISPOSAL", "TRANSFER"]


class InventoryProvision(BaseModel):
    variant_id: uuid.UUID


class AdjustmentCreate(BaseModel):
    """One stock movement, expressed the same way the ledger stores it.

    ``quantity`` is always a positive magnitude and the buckets carry the
    direction - inbound names only ``to_bucket``, outbound only
    ``from_bucket``, a transfer both. Bounds are checked in the service so
    that a bad quantity answers with INVALID_INVENTORY_ADJUSTMENT rather
    than a generic validation error.
    """

    transaction_type: AdjustableType
    quantity: int
    from_bucket: Bucket | None = None
    to_bucket: Bucket | None = None
    reason: str


class InventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variant_id: uuid.UUID
    quantity_on_hand: int
    quantity_reserved: int
    quantity_damaged: int
    quantity_quarantined: int
    # Derived, never stored: quantity_on_hand - quantity_reserved.
    available: int
    created_at: datetime
    updated_at: datetime


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transaction_type: TransactionType
    quantity: int
    from_bucket: Bucket | None
    to_bucket: Bucket | None
    reference_type: str | None
    reference_id: uuid.UUID | None
    created_at: datetime


class ReservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cart_id: uuid.UUID
    quantity: int
    status: ReservationStatus
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
