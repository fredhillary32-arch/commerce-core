"""Request and response shapes for saved addresses. No queries here.

Note what no request carries: a ``user_id``. The owner is resolved from the
access token, which is what makes it impossible to reach another customer's
address book by editing a payload.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AddressCreate(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    recipient_name: str = Field(min_length=1, max_length=255)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=100)
    postal_code: str = Field(min_length=1, max_length=32)
    # Exactly two characters, ISO 3166-1 alpha-2. Length is checked here
    # because a three-letter code is a malformed request, not a business
    # rule the service should have an opinion about; case is normalised in
    # the service so "gb" and "GB" resolve to the same tax rule.
    country_code: str = Field(min_length=2, max_length=2)
    phone: str | None = Field(default=None, max_length=32)
    is_default: bool = False


class AddressUpdate(BaseModel):
    """A partial change. Every field is optional; absent means "leave it".

    ``is_default`` accepts only ``True``: promoting a different address is
    how a default moves, and clearing the only one would leave the customer
    without the convenience the flag exists to provide.
    """

    label: str | None = Field(default=None, max_length=100)
    recipient_name: str | None = Field(default=None, min_length=1, max_length=255)
    line1: str | None = Field(default=None, min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, min_length=1, max_length=32)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    phone: str | None = Field(default=None, max_length=32)
    is_default: bool | None = None


class AddressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str | None
    recipient_name: str
    line1: str
    line2: str | None
    city: str
    region: str | None
    postal_code: str
    country_code: str
    phone: str | None
    is_default: bool
    created_at: datetime
    updated_at: datetime
