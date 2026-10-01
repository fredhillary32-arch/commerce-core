"""A customer's cart and the variants they intend to buy.

Two things are deliberately absent.

There is no price column on a cart item. What a variant costs is read from
the catalogue at display time; the permanent price/name/SKU snapshot that
survives a later price change belongs to order items, not here, and adding
one "for convenience" would quietly create a second source of truth.

There is no link to inventory. Putting something in a cart reserves nothing:
cart state and reservation state are separate concerns, and conflating them
would let an abandoned cart strand stock indefinitely.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CART_ACTIVE = "ACTIVE"
CART_ABANDONED = "ABANDONED"
CART_CONVERTED = "CONVERTED"
CART_STATUSES = (CART_ACTIVE, CART_ABANDONED, CART_CONVERTED)


class Cart(Base):
    """One customer's basket. At most one may be ACTIVE per user."""

    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in CART_STATUSES) + ")",
            name="status_valid",
        ),
        # Partial, exactly like the catalogue's one-primary-image-per-product
        # index: a user may accumulate any number of CONVERTED or ABANDONED
        # carts as history, but never two live ones. This is also what makes
        # get-or-create safe under concurrency - the losing INSERT of a race
        # is rejected here rather than producing a second cart.
        Index(
            "uq_carts_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"status = '{CART_ACTIVE}'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{CART_ACTIVE}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CartItem(Base):
    """One variant in one cart, with how many of it the customer wants.

    Items reference variants and never products: a product is not a thing a
    warehouse can pick.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # Backs the "adding a variant already present increments it" rule.
        # The service does the increment; this makes a duplicate row
        # impossible even if a future caller forgets to.
        UniqueConstraint("cart_id", "variant_id", name="uq_cart_items_cart_id_variant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("carts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # RESTRICT rather than CASCADE: a variant someone is holding in a cart
    # must not be deletable out from under them. The catalogue's own deletion
    # guard refuses first; this is the guarantee underneath it.
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
