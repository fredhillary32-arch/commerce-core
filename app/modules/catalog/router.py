"""Catalogue HTTP endpoints.

Four routers rather than one, because the catalogue is addressed from four
roots: the public storefront under /catalog, and the admin resources
/products, /variants and /categories. Splitting them also makes the
authorization story legible - nothing on ``public_router`` takes a guard,
and nothing on the other three is without one.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.modules.audit.service import ClientInfo
from app.modules.catalog import service
from app.modules.catalog.dependencies import CatalogReader, CatalogWriter, DbSession, ImageWriter
from app.modules.catalog.schemas import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    ImageConfirm,
    ImageRead,
    OptionCreate,
    OptionRead,
    OptionValueCreate,
    OptionValueRead,
    ProductCategoryAssign,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    PublicCategoryDetail,
    PublicCategoryRead,
    PublicProductRead,
    PublicProductSummary,
    UploadTargetRead,
    VariantCreate,
    VariantOptionValueAssign,
    VariantRead,
    VariantUpdate,
)

public_router = APIRouter(prefix="/catalog", tags=["catalog"])
products_router = APIRouter(prefix="/products", tags=["catalog-admin"])
variants_router = APIRouter(prefix="/variants", tags=["catalog-admin"])
categories_router = APIRouter(prefix="/categories", tags=["catalog-admin"])

# A listing is always bounded: an unpaged catalogue endpoint degrades
# silently as the catalogue grows, and each product costs a signed URL.
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


# --------------------------------------------------------------------------
# Public storefront - no authentication, ACTIVE products and active
# categories only.
# --------------------------------------------------------------------------


@public_router.get("/products", response_model=list[PublicProductSummary])
def list_public_products(db: DbSession, limit: Limit = 50, offset: Offset = 0):
    return service.list_public_products(db, limit=limit, offset=offset)


@public_router.get("/products/{product_id}", response_model=PublicProductRead)
def read_public_product(product_id: uuid.UUID, db: DbSession):
    return service.get_public_product(db, product_id)


@public_router.get("/categories", response_model=list[PublicCategoryRead])
def list_public_categories(db: DbSession):
    return service.list_public_categories(db)


@public_router.get("/categories/{category_id}", response_model=PublicCategoryDetail)
def read_public_category(category_id: uuid.UUID, db: DbSession):
    return service.get_public_category(db, category_id)


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


@products_router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate, request: Request, admin: CatalogWriter, db: DbSession
):
    product = service.create_product(
        db,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_product_view(db, product, public=False)


@products_router.get("/{product_id}", response_model=ProductRead)
def read_product(product_id: uuid.UUID, admin: CatalogReader, db: DbSession):
    product = service.require_product(db, product_id)
    return service.build_product_view(db, product, public=False)


@products_router.patch("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    request: Request,
    admin: CatalogWriter,
    db: DbSession,
):
    product = service.update_product(
        db,
        product_id=product_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_product_view(db, product, public=False)


@products_router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: uuid.UUID, admin: CatalogWriter, db: DbSession) -> None:
    """Not in the Phase 3 endpoint list, but the deletion guard it enforces is
    specified, and a rule with no way to exercise it cannot be relied on."""
    service.delete_product(db, product_id=product_id)


@products_router.post(
    "/{product_id}/options", response_model=OptionRead, status_code=status.HTTP_201_CREATED
)
def create_option(
    product_id: uuid.UUID, payload: OptionCreate, admin: CatalogWriter, db: DbSession
):
    option = service.create_option(db, product_id=product_id, name=payload.name)
    return {"id": option.id, "name": option.name, "values": []}


@products_router.post(
    "/{product_id}/options/{option_id}/values",
    response_model=OptionValueRead,
    status_code=status.HTTP_201_CREATED,
)
def create_option_value(
    product_id: uuid.UUID,
    option_id: uuid.UUID,
    payload: OptionValueCreate,
    admin: CatalogWriter,
    db: DbSession,
):
    return service.create_option_value(
        db, product_id=product_id, option_id=option_id, value=payload.value
    )


@products_router.post(
    "/{product_id}/variants", response_model=VariantRead, status_code=status.HTTP_201_CREATED
)
def create_variant(
    product_id: uuid.UUID,
    payload: VariantCreate,
    request: Request,
    admin: CatalogWriter,
    db: DbSession,
):
    variant = service.create_variant(
        db,
        product_id=product_id,
        sku=payload.sku,
        price=payload.price,
        currency=payload.currency,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_variant_view(db, variant)


@products_router.post(
    "/{product_id}/categories", status_code=status.HTTP_204_NO_CONTENT
)
def assign_product_to_category(
    product_id: uuid.UUID,
    payload: ProductCategoryAssign,
    admin: CatalogWriter,
    db: DbSession,
) -> None:
    service.assign_product_to_category(
        db, product_id=product_id, category_id=payload.category_id
    )


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------


@variants_router.patch("/{variant_id}", response_model=VariantRead)
def update_variant(
    variant_id: uuid.UUID,
    payload: VariantUpdate,
    request: Request,
    admin: CatalogWriter,
    db: DbSession,
):
    variant = service.update_variant(
        db,
        variant_id=variant_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_variant_view(db, variant)


@variants_router.post(
    "/{variant_id}/option-values", response_model=VariantRead, status_code=status.HTTP_201_CREATED
)
def assign_option_value(
    variant_id: uuid.UUID,
    payload: VariantOptionValueAssign,
    request: Request,
    admin: CatalogWriter,
    db: DbSession,
):
    variant = service.assign_option_value(
        db,
        variant_id=variant_id,
        option_id=payload.option_id,
        option_value_id=payload.option_value_id,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_variant_view(db, variant)


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


@categories_router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate, request: Request, admin: CatalogWriter, db: DbSession
):
    return service.create_category(
        db,
        name=payload.name,
        slug=payload.slug,
        parent_id=payload.parent_id,
        actor=admin,
        client=ClientInfo.from_request(request),
    )


@categories_router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    request: Request,
    admin: CatalogWriter,
    db: DbSession,
):
    return service.update_category(
        db,
        category_id=category_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=admin,
        client=ClientInfo.from_request(request),
    )


@categories_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: uuid.UUID, admin: CatalogWriter, db: DbSession) -> None:
    """Added for the same reason as product deletion: Phase 3 specifies when a
    category may be removed, so there has to be a way to attempt it."""
    service.delete_category(db, category_id=category_id)


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


@products_router.post(
    "/{product_id}/images/upload-url",
    response_model=UploadTargetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_image_upload_target(product_id: uuid.UUID, admin: ImageWriter, db: DbSession):
    storage_path, upload_url = service.create_image_upload_target(db, product_id=product_id)
    return {"storage_path": storage_path, "upload_url": upload_url}


@products_router.post(
    "/{product_id}/images/confirm", response_model=ImageRead, status_code=status.HTTP_201_CREATED
)
def confirm_image(
    product_id: uuid.UUID,
    payload: ImageConfirm,
    request: Request,
    admin: ImageWriter,
    db: DbSession,
):
    image = service.confirm_image(
        db,
        product_id=product_id,
        storage_path=payload.storage_path,
        alt_text=payload.alt_text,
        sort_order=payload.sort_order,
        is_primary=payload.is_primary,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.image_view(image, public=False)


@products_router.delete(
    "/{product_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_image(
    product_id: uuid.UUID,
    image_id: uuid.UUID,
    request: Request,
    admin: ImageWriter,
    db: DbSession,
) -> None:
    service.delete_image(
        db,
        product_id=product_id,
        image_id=image_id,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
