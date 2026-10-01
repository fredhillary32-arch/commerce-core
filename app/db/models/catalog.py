"""The product catalogue: products, options, variants, categories, images.

Inventory quantities are deliberately absent - whether something is *in
stock* is a separate concern from what the catalogue describes, and belongs
to its own phase.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PRODUCT_DRAFT = "DRAFT"
PRODUCT_ACTIVE = "ACTIVE"
PRODUCT_ARCHIVED = "ARCHIVED"
PRODUCT_DISCONTINUED = "DISCONTINUED"
PRODUCT_STATUSES = (PRODUCT_DRAFT, PRODUCT_ACTIVE, PRODUCT_ARCHIVED, PRODUCT_DISCONTINUED)

VARIANT_DRAFT = "DRAFT"
VARIANT_ACTIVE = "ACTIVE"
VARIANT_DISCONTINUED = "DISCONTINUED"
VARIANT_STATUSES = (VARIANT_DRAFT, VARIANT_ACTIVE, VARIANT_DISCONTINUED)


def _status_check(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{status}'" for status in statuses) + ")"


class Product(Base):
    """A sellable thing as the catalogue describes it, independent of stock."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(_status_check(PRODUCT_STATUSES), name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Unique, and therefore already indexed. Collisions are reported to the
    # caller rather than silently resolved with a numeric suffix, so a URL is
    # never quietly different from the one the merchant asked for.
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{PRODUCT_DRAFT}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProductOption(Base):
    """A dimension a product varies along, e.g. "Size"."""

    __tablename__ = "product_options"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProductOptionValue(Base):
    """One choice within an option, e.g. "Large".

    A value belongs to exactly one option and therefore, transitively, to one
    product: no extra constraint is needed to say so, because the schema
    makes any other arrangement impossible to represent.
    """

    __tablename__ = "product_option_values"
    __table_args__ = (
        # Redundant on its own (id is already the primary key), but required
        # as the target of the composite foreign key on variant_option_values,
        # which is what stops an option and a value from disagreeing.
        UniqueConstraint("option_id", "id", name="uq_product_option_values_option_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    option_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    value: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProductVariant(Base):
    """One concretely purchasable configuration of a product."""

    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint(_status_check(VARIANT_STATUSES), name="status_valid"),
        # Only constrains rows that have a price at all: price stays null
        # while a variant is DRAFT, and the service requires it before ACTIVE.
        CheckConstraint("price IS NULL OR price > 0", name="price_positive"),
        # The whole point of variant_signature. Because the column is null
        # until the variant has at least one option-value assignment, and
        # PostgreSQL treats every null as distinct here, any number of
        # not-yet-configured draft variants coexist on one product while two
        # fully configured ones can never share a combination.
        UniqueConstraint(
            "product_id", "variant_signature", name="uq_product_variants_product_id_signature"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Merchant-supplied and compared exactly as given, including case: two
    # SKUs differing only in case are two different SKUs to a warehouse.
    sku: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{VARIANT_DRAFT}'")
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    variant_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class VariantOptionValue(Base):
    """Which value a variant takes for one of its product's options.

    ``option_id`` duplicates information already reachable through
    ``option_value_id``. That denormalisation is deliberate and is the only
    one in the catalogue: it turns "at most one value per option per variant"
    into a plain unique constraint instead of a trigger, and the composite
    foreign key below guarantees the two columns can never contradict
    each other.
    """

    __tablename__ = "variant_option_values"
    __table_args__ = (
        UniqueConstraint(
            "variant_id", "option_id", name="uq_variant_option_values_variant_id_option_id"
        ),
        ForeignKeyConstraint(
            ["option_id", "option_value_id"],
            ["product_option_values.option_id", "product_option_values.id"],
            name="fk_variant_option_values_option_id_value_id",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    option_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    option_value_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Category(Base):
    """A node in the merchandising tree.

    The hierarchy is a plain adjacency list; cycles are prevented by the
    service walking the ancestor chain, because no practical PostgreSQL
    constraint expresses "this edge must not close a loop".
    """

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProductCategory(Base):
    """Membership of a product in a category. Indexed in both directions."""

    __tablename__ = "product_categories"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProductImage(Base):
    """Metadata for one image file held in Supabase Storage.

    ``storage_path`` is the only link to the file; the delivery URL is signed
    on demand and never stored, because the bucket is private and every
    generated URL expires.
    """

    __tablename__ = "product_images"
    __table_args__ = (
        # A partial unique index rather than a constraint: "at most one
        # primary" must not stop a product having many non-primary images.
        Index(
            "uq_product_images_one_primary_per_product",
            "product_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    alt_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
