"""Request and response shapes for shipping rules. No queries here."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ShippingRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    country_code: str = Field(min_length=2, max_length=2)
    min_order_subtotal: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    # Null for an open-ended top band. The band's validity as a *pair* is
    # left to the CHECK constraint, so the rule holds even against a direct
    # write, and the service translates the violation.
    max_order_subtotal: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    cost: Decimal = Field(ge=0, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    priority: int = 0
    is_active: bool = True


class ShippingRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    min_order_subtotal: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    max_order_subtotal: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    cost: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    priority: int | None = None
    is_active: bool | None = None


class ShippingRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    country_code: str
    min_order_subtotal: Decimal
    max_order_subtotal: Decimal | None
    cost: Decimal
    currency: str
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
