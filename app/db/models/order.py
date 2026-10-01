"""Orders and their lines: what a customer bought, frozen at the moment they bought it.

Two convictions shape these tables.

First, **an order is a snapshot, not a set of pointers.** Product name, SKU,
option summary, unit price, tax rate and both addresses are copied onto the
order. Renaming a product, repricing a variant, changing a tax rate or
deleting a saved address can never alter what a past order says. The only
foreign keys that remain are the ones that carry *identity* rather than
display - ``variant_id`` for reorders and returns, ``user_id`` for the
buyer, ``cart_id`` for the reservations - and each of those is RESTRICT
precisely so history stays readable.

Second, **the arithmetic is the database's job wherever one row can check
it.** Line totals and order totals are enforced by CHECK constraints, so a
service bug cannot persist an order whose numbers do not add up. The one
thing a CHECK cannot express is the cross-row sum of lines against the
order header; that stays a service-level assertion with a test behind it,
the same compromise the catalogue makes for category cycles.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ORDER_PENDING_PAYMENT = "PENDING_PAYMENT"
ORDER_CONFIRMED = "CONFIRMED"
ORDER_CANCELLED = "CANCELLED"
ORDER_STATUSES = (ORDER_PENDING_PAYMENT, ORDER_CONFIRMED, ORDER_CANCELLED)

_STATUS_LIST = ", ".join(f"'{status}'" for status in ORDER_STATUSES)


class Order(Base):
    """One purchase, from placement to confirmation or cancellation.

    **There is no payment column.** ``status`` *is* the payment boundary:
    an order is PENDING_PAYMENT until Phase 6 confirms it. A
    ``payment_status`` column added now would be Phase 6 schema shipped
    early with nothing to write it.

    ``cart_id`` does double duty. It is the route to this order's
    reservations - ``inventory_reservations.cart_id`` is Phase 4's direct
    foreign key, reused rather than duplicated as an ``order_id`` - and,
    being UNIQUE, it is also the idempotency key: one cart can produce
    exactly one order, so a double-submitted checkout cannot buy twice.
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_LIST})", name="status_valid"),
        CheckConstraint("subtotal_amount >= 0", name="subtotal_amount_non_negative"),
        CheckConstraint("discount_total >= 0", name="discount_total_non_negative"),
        CheckConstraint("tax_total >= 0", name="tax_total_non_negative"),
        CheckConstraint("shipping_total >= 0", name="shipping_total_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        # The header arithmetic, enforced where it cannot be bypassed. A
        # service that computed a total wrongly would be refused by
        # PostgreSQL rather than quietly billing the wrong amount.
        CheckConstraint(
            "total_amount = subtotal_amount - discount_total + tax_total + shipping_total",
            name="totals_reconcile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Customer-facing, and deliberately not sequential: a sequence lets
    # anyone place two orders and read the merchant's volume from the gap
    # between them. Generated in the application from an alphabet with no
    # visually ambiguous characters, because a customer may read it aloud.
    reference: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    # RESTRICT: an order must always be able to name a real buyer, so a user
    # who has ordered cannot simply be deleted.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("carts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{ORDER_PENDING_PAYMENT}'")
    )
    # One currency per order. A cart mixing currencies is rejected at
    # checkout rather than converted, because this system holds no exchange
    # rates and inventing one would be worse than refusing.
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Always zero in this phase. The column exists so that promotions, when
    # they arrive, do not require a migration of live order history.
    discount_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    shipping_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Snapshots, with no foreign key to `addresses` on purpose: a customer
    # editing or deleting a saved address must not rewrite where a past
    # order was sent.
    shipping_address: Mapped[dict] = mapped_column(JSONB, nullable=False)
    billing_address: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Which rule decided the money, kept so a total can be explained years
    # later even after the rule row has been retired or amended.
    tax_rate_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    shipping_rule_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # When this order's stock hold lapses. Nothing sweeps it in this phase -
    # workers are Phase 8 - so it exists to let the API tell a customer
    # their hold has expired instead of silently failing at payment.
    reservation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OrderItem(Base):
    """One variant bought on one order, with everything about it frozen.

    Each snapshot column answers a specific way the catalogue could later
    make the order unreadable: the product is renamed, the SKU is reassigned
    to something else, the options are deleted, the price changes, the tax
    rate is amended. ``variant_id`` survives as identity so a customer can
    reorder or return, and it is RESTRICT so that deleting the variant
    cannot orphan the line.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("discount_amount >= 0", name="discount_amount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
        CheckConstraint("tax_rate >= 0", name="tax_rate_non_negative"),
        CheckConstraint("line_subtotal >= 0", name="line_subtotal_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        CheckConstraint("line_subtotal = unit_price * quantity", name="line_subtotal_matches"),
        CheckConstraint(
            "line_total = line_subtotal - discount_amount + tax_amount",
            name="line_total_matches",
        ),
        # Mirrors uq_cart_items_cart_id_variant_id. The cart cannot hold two
        # lines for one variant, so neither can the order it became.
        UniqueConstraint("order_id", "variant_id", name="uq_order_items_order_id_variant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # No foreign key: a product may legitimately be purged from the
    # catalogue one day, and the order must still say which product this
    # was. The name beside it is what makes the id readable.
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    variant_option_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    tax_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default=text("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    line_subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
