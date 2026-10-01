"""Request and response shapes for tax rates. No queries here."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TaxRateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # A fraction, not a percentage: 0.2000 is 20%. Bounded here as well as
    # by the CHECK constraint, so a nonsensical rate is a validation error
    # the caller can read rather than a 500 from an unmapped constraint.
    rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    country_code: str = Field(min_length=2, max_length=2)
    # Null means the rate covers the whole country.
    region: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class TaxRateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    rate: Decimal | None = Field(default=None, ge=0, le=1, decimal_places=4)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class TaxRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    rate: Decimal
    country_code: str
    region: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
