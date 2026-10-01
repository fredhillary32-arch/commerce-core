"""Every catalogue database query. No business rules live here."""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Row, delete, func, select
from sqlalchemy.orm import Session

from app.db.models.catalog import (
    PRODUCT_ACTIVE,
    VARIANT_ACTIVE,
    Category,
    Product,
    ProductCategory,
    ProductImage,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    VariantOptionValue,
)

# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


def get_product(db: Session, product_id: uuid.UUID) -> Product | None:
    return db.get(Product, product_id)


def add_product(db: Session, *, name: str, slug: str, description: str | None) -> Product:
    product = Product(name=name, slug=slug, description=description)
    db.add(product)
    db.flush()
    return product


def list_products(
    db: Session, *, statuses: Sequence[str] | None = None, limit: int, offset: int
) -> list[Product]:
    statement = select(Product)
    if statuses is not None:
        statement = statement.where(Product.status.in_(statuses))
    statement = statement.order_by(Product.created_at.desc(), Product.id).limit(limit).offset(offset)
    return list(db.execute(statement).scalars())


def delete_product(db: Session, product: Product) -> None:
    db.delete(product)


def count_variants(db: Session, product_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(ProductVariant).where(ProductVariant.product_id == product_id)
    ).scalar_one()


def count_category_assignments(db: Session, product_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(ProductCategory)
        .where(ProductCategory.product_id == product_id)
    ).scalar_one()


def count_images(db: Session, product_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(ProductImage).where(ProductImage.product_id == product_id)
    ).scalar_one()


def has_active_variant(db: Session, product_id: uuid.UUID) -> bool:
    return (
        db.execute(
            select(ProductVariant.id)
            .where(
                ProductVariant.product_id == product_id,
                ProductVariant.status == VARIANT_ACTIVE,
            )
            .limit(1)
        ).first()
        is not None
    )


# --------------------------------------------------------------------------
# Options and option values
# --------------------------------------------------------------------------


def get_option(db: Session, option_id: uuid.UUID) -> ProductOption | None:
    return db.get(ProductOption, option_id)


def add_option(db: Session, *, product_id: uuid.UUID, name: str) -> ProductOption:
    option = ProductOption(product_id=product_id, name=name)
    db.add(option)
    db.flush()
    return option


def list_options(db: Session, product_id: uuid.UUID) -> list[ProductOption]:
    return list(
        db.execute(
            select(ProductOption)
            .where(ProductOption.product_id == product_id)
            .order_by(ProductOption.created_at, ProductOption.id)
        ).scalars()
    )


def get_option_value(db: Session, option_value_id: uuid.UUID) -> ProductOptionValue | None:
    return db.get(ProductOptionValue, option_value_id)


def add_option_value(db: Session, *, option_id: uuid.UUID, value: str) -> ProductOptionValue:
    option_value = ProductOptionValue(option_id=option_id, value=value)
    db.add(option_value)
    db.flush()
    return option_value


def list_option_values_for_product(db: Session, product_id: uuid.UUID) -> list[ProductOptionValue]:
    return list(
        db.execute(
            select(ProductOptionValue)
            .join(ProductOption, ProductOption.id == ProductOptionValue.option_id)
            .where(ProductOption.product_id == product_id)
            .order_by(ProductOptionValue.created_at, ProductOptionValue.id)
        ).scalars()
    )


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------


def get_variant(db: Session, variant_id: uuid.UUID) -> ProductVariant | None:
    return db.get(ProductVariant, variant_id)


def add_variant(
    db: Session,
    *,
    product_id: uuid.UUID,
    sku: str,
    price: Decimal | None,
    currency: str | None,
) -> ProductVariant:
    variant = ProductVariant(product_id=product_id, sku=sku, price=price, currency=currency)
    db.add(variant)
    db.flush()
    return variant


def list_variants(
    db: Session, product_id: uuid.UUID, *, statuses: Sequence[str] | None = None
) -> list[ProductVariant]:
    statement = select(ProductVariant).where(ProductVariant.product_id == product_id)
    if statuses is not None:
        statement = statement.where(ProductVariant.status.in_(statuses))
    return list(
        db.execute(statement.order_by(ProductVariant.created_at, ProductVariant.id)).scalars()
    )


def list_assignments(db: Session, variant_id: uuid.UUID) -> list[VariantOptionValue]:
    return list(
        db.execute(
            select(VariantOptionValue)
            .where(VariantOptionValue.variant_id == variant_id)
            .order_by(VariantOptionValue.option_id)
        ).scalars()
    )


def add_assignment(
    db: Session, *, variant_id: uuid.UUID, option_id: uuid.UUID, option_value_id: uuid.UUID
) -> VariantOptionValue:
    assignment = VariantOptionValue(
        variant_id=variant_id, option_id=option_id, option_value_id=option_value_id
    )
    db.add(assignment)
    db.flush()
    return assignment


def list_assignment_details(db: Session, variant_ids: Sequence[uuid.UUID]) -> list[Row]:
    """Every assignment of the given variants, with option and value names.

    One query for the whole product rather than one per variant, so building
    a product response stays flat regardless of how many variants it has.
    """
    if not variant_ids:
        return []
    return list(
        db.execute(
            select(
                VariantOptionValue.variant_id,
                VariantOptionValue.option_id,
                ProductOption.name.label("option_name"),
                VariantOptionValue.option_value_id,
                ProductOptionValue.value.label("value"),
            )
            .join(ProductOption, ProductOption.id == VariantOptionValue.option_id)
            .join(ProductOptionValue, ProductOptionValue.id == VariantOptionValue.option_value_id)
            .where(VariantOptionValue.variant_id.in_(variant_ids))
            .order_by(ProductOption.name, ProductOptionValue.value)
        ).all()
    )


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


def get_category(db: Session, category_id: uuid.UUID) -> Category | None:
    return db.get(Category, category_id)


def add_category(
    db: Session, *, name: str, slug: str, parent_id: uuid.UUID | None
) -> Category:
    category = Category(name=name, slug=slug, parent_id=parent_id)
    db.add(category)
    db.flush()
    return category


def list_categories(db: Session, *, active_only: bool) -> list[Category]:
    statement = select(Category)
    if active_only:
        statement = statement.where(Category.is_active.is_(True))
    return list(db.execute(statement.order_by(Category.name, Category.id)).scalars())


def delete_category(db: Session, category: Category) -> None:
    db.delete(category)


def count_child_categories(db: Session, category_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(Category).where(Category.parent_id == category_id)
    ).scalar_one()


def count_products_in_category(db: Session, category_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(ProductCategory)
        .where(ProductCategory.category_id == category_id)
    ).scalar_one()


def get_product_category(
    db: Session, product_id: uuid.UUID, category_id: uuid.UUID
) -> ProductCategory | None:
    return db.get(ProductCategory, (product_id, category_id))


def add_product_category(
    db: Session, *, product_id: uuid.UUID, category_id: uuid.UUID
) -> ProductCategory:
    membership = ProductCategory(product_id=product_id, category_id=category_id)
    db.add(membership)
    db.flush()
    return membership


def list_categories_for_product(
    db: Session, product_id: uuid.UUID, *, active_only: bool
) -> list[Category]:
    statement = (
        select(Category)
        .join(ProductCategory, ProductCategory.category_id == Category.id)
        .where(ProductCategory.product_id == product_id)
    )
    if active_only:
        statement = statement.where(Category.is_active.is_(True))
    return list(db.execute(statement.order_by(Category.name, Category.id)).scalars())


def list_active_product_ids_in_category(db: Session, category_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(Product.id)
            .join(ProductCategory, ProductCategory.product_id == Product.id)
            .where(ProductCategory.category_id == category_id, Product.status == PRODUCT_ACTIVE)
            .order_by(Product.name)
        ).scalars()
    )


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def get_image(db: Session, image_id: uuid.UUID) -> ProductImage | None:
    return db.get(ProductImage, image_id)


def add_image(
    db: Session,
    *,
    product_id: uuid.UUID,
    storage_path: str,
    alt_text: str | None,
    sort_order: int,
    is_primary: bool,
) -> ProductImage:
    image = ProductImage(
        product_id=product_id,
        storage_path=storage_path,
        alt_text=alt_text,
        sort_order=sort_order,
        is_primary=is_primary,
    )
    db.add(image)
    db.flush()
    return image


def remove_image(db: Session, image_id: uuid.UUID) -> None:
    db.execute(delete(ProductImage).where(ProductImage.id == image_id))


def list_images(db: Session, product_id: uuid.UUID) -> list[ProductImage]:
    return list(
        db.execute(
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.sort_order, ProductImage.created_at, ProductImage.id)
        ).scalars()
    )


def get_primary_image(db: Session, product_id: uuid.UUID) -> ProductImage | None:
    return db.execute(
        select(ProductImage).where(
            ProductImage.product_id == product_id, ProductImage.is_primary.is_(True)
        )
    ).scalar_one_or_none()
