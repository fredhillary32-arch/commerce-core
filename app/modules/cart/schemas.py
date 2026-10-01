"""Request and response shapes for the cart. No queries here.

Note what no request carries: a cart id, or a user id. Both are resolved
from the access token, which is what makes it impossible to address another
customer's cart by editing a payload.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

CartStatus = Literal["ACTIVE", "ABANDONED", "CONVERTED"]
UnavailableReason = Literal["VARIANT_NOT_ACTIVE", "PRODUCT_NOT_ACTIVE"]


class CartItemAdd(BaseModel):
    variant_id: uuid.UUID
    # Bounds are checked in the service rather than here, so that a bad
    # quantity answers with INVALID_CART_ITEM_QUANTITY instead of a generic
    # validation error the caller cannot act on as precisely.
    quantity: int = 1


class CartItemUpdate(BaseModel):
    quantity: int


class CartItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variant_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    sku: str
    quantity: int
    # Null while the variant is still being configured in the catalogue. A
    # line like that is also flagged unavailable, so no caller has to treat
    # a missing price as a purchasable state.
    unit_price: Decimal | None
    currency: str | None
    available: bool
    unavailable_reason: UnavailableReason | None


class CartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: CartStatus
    items: list[CartItemRead]
    created_at: datetime
    updated_at: datetime
