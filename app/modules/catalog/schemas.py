"""Request and response shapes for the catalogue. No queries here."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Spelled out rather than derived from the model constants so an invalid
# status is a 422 from request validation, with the accepted values visible
# in the generated OpenAPI schema. A schema test keeps the two in step.
ProductStatus = Literal["DRAFT", "ACTIVE", "ARCHIVED", "DISCONTINUED"]
VariantStatus = Literal["DRAFT", "ACTIVE", "DISCONTINUED"]


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Optional: derived from the name when omitted. Supplying it is how a
    # caller resolves a slug collision, since the server never invents a
    # suffix of its own.
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: ProductStatus | None = None


class OptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class OptionValueCreate(BaseModel):
    value: str = Field(min_length=1, max_length=100)


class VariantCreate(BaseModel):
    # Compared exactly as supplied, including case.
    sku: str = Field(min_length=1, max_length=100)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class VariantUpdate(BaseModel):
    sku: str | None = Field(default=None, min_length=1, max_length=100)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: VariantStatus | None = None


class VariantOptionValueAssign(BaseModel):
    option_id: uuid.UUID
    option_value_id: uuid.UUID


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None


class ProductCategoryAssign(BaseModel):
    category_id: uuid.UUID


class ImageConfirm(BaseModel):
    # The exact path handed back by the upload-url endpoint. The server
    # re-derives nothing from it beyond checking it belongs to this product.
    storage_path: str = Field(min_length=1, max_length=500)
    alt_text: str | None = Field(default=None, max_length=255)
    sort_order: int = 0
    is_primary: bool = False


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


class OptionValueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    option_id: uuid.UUID
    value: str


class OptionRead(BaseModel):
    id: uuid.UUID
    name: str
    values: list[OptionValueRead]


class VariantOptionValueRead(BaseModel):
    option_id: uuid.UUID
    option_name: str
    option_value_id: uuid.UUID
    value: str


class VariantRead(BaseModel):
    id: uuid.UUID
    sku: str
    status: str
    price: Decimal | None
    currency: str | None
    option_values: list[VariantOptionValueRead]


class ImageRead(BaseModel):
    id: uuid.UUID
    storage_path: str
    # Freshly signed on every response; short-lived and not safe to cache.
    url: str
    alt_text: str | None
    sort_order: int
    is_primary: bool


class PublicImageRead(BaseModel):
    id: uuid.UUID
    url: str
    alt_text: str | None
    sort_order: int
    is_primary: bool


class CategorySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    parent_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PublicCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    parent_id: uuid.UUID | None


class PublicCategoryDetail(PublicCategoryRead):
    product_ids: list[uuid.UUID]


class ProductRead(BaseModel):
    """Admin view: every status, every variant, storage paths included."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    options: list[OptionRead]
    variants: list[VariantRead]
    images: list[ImageRead]
    categories: list[CategorySummary]


class UploadTargetRead(BaseModel):
    storage_path: str
    upload_url: str


class PublicProductSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    # Only the primary image, so a listing signs one URL per product.
    primary_image_url: str | None


class PublicProductRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    options: list[OptionRead]
    variants: list[VariantRead]
    images: list[PublicImageRead]
    categories: list[CategorySummary]
