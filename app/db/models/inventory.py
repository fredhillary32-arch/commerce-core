"""Stock levels, temporary holds on them, and the ledger that explains both.

The four quantity columns are **disjoint buckets**, not nested ones.
``quantity_on_hand`` is good, sellable stock; damaged and quarantined units
sit alongside it rather than inside it. Total physical stock in custody is
therefore ``on_hand + damaged + quarantined``, and moving a unit from
ON_HAND to DAMAGED genuinely decrements one column and increments the other.

That is what lets the ledger reconcile exactly: for every bucket, the sum of
transactions into it minus the sum out of it equals its current column
value, with a TRANSFER counting as an outbound from one bucket and an
inbound to the other. A nested model - where damaged units were also
counted in on-hand - could not satisfy that, because a transfer would have
to leave on-hand untouched while still recording an outbound from it.
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
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RESERVATION_ACTIVE = "ACTIVE"
RESERVATION_CONVERTED = "CONVERTED"
RESERVATION_EXPIRED = "EXPIRED"
RESERVATION_RELEASED = "RELEASED"
RESERVATION_STATUSES = (
    RESERVATION_ACTIVE,
    RESERVATION_CONVERTED,
    RESERVATION_EXPIRED,
    RESERVATION_RELEASED,
)

BUCKET_ON_HAND = "ON_HAND"
BUCKET_DAMAGED = "DAMAGED"
BUCKET_QUARANTINED = "QUARANTINED"
BUCKETS = (BUCKET_ON_HAND, BUCKET_DAMAGED, BUCKET_QUARANTINED)

TRANSACTION_PURCHASE = "PURCHASE"
TRANSACTION_RESTOCK = "RESTOCK"
TRANSACTION_RETURN = "RETURN"
TRANSACTION_ADJUSTMENT = "ADJUSTMENT"
TRANSACTION_DISPOSAL = "DISPOSAL"
TRANSACTION_TRANSFER = "TRANSFER"
TRANSACTION_TYPES = (
    TRANSACTION_PURCHASE,
    TRANSACTION_RESTOCK,
    TRANSACTION_RETURN,
    TRANSACTION_ADJUSTMENT,
    TRANSACTION_DISPOSAL,
    TRANSACTION_TRANSFER,
)

# Which column each bucket name addresses. One mapping, used by the
# adjustment path and by the reconciliation query alike, so a bucket can
# never be applied to the wrong column in one place and the right one in
# another.
BUCKET_COLUMNS = {
    BUCKET_ON_HAND: "quantity_on_hand",
    BUCKET_DAMAGED: "quantity_damaged",
    BUCKET_QUARANTINED: "quantity_quarantined",
}


def _in_list(values: tuple[str, ...], column: str) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class Inventory(Base):
    """How much of one variant exists, and how much of it is spoken for.

    **Read this before using ``quantity_on_hand``.** Despite its name it is
    *not* a physical stock-take figure:

    * ``quantity_on_hand`` is **good, sellable stock**, before reservation.
    * ``quantity_damaged`` and ``quantity_quarantined`` are **separate
      physical buckets alongside it**, not subsets of it.
    * ``available = quantity_on_hand - quantity_reserved``
    * **total physical stock = on_hand + damaged + quarantined**

    So a warehouse stock-take matches the **sum of the three columns**, not
    ``quantity_on_hand`` alone. The column keeps its name because renaming
    it would churn the schema, the migration and every caller; this
    docstring is the correction.

    Damaged and quarantined stock can never be reserved: it is not in
    ``quantity_on_hand`` to begin with, and ``quantity_reserved`` may not
    exceed ``quantity_on_hand``.

    Exactly one row per variant. Having a row does not make a variant
    sellable - catalogue status still governs that, and DRAFT or
    DISCONTINUED variants never take part in reservation workflows.
    """

    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity_on_hand >= 0", name="quantity_on_hand_non_negative"),
        CheckConstraint("quantity_reserved >= 0", name="quantity_reserved_non_negative"),
        CheckConstraint("quantity_damaged >= 0", name="quantity_damaged_non_negative"),
        CheckConstraint("quantity_quarantined >= 0", name="quantity_quarantined_non_negative"),
        # Named deliberately, because the adjustment service maps exactly
        # this constraint's violation to INVALID_INVENTORY_ADJUSTMENT. It is
        # what stops a stock reduction from silently overselling reservations
        # that have already been promised to a customer.
        CheckConstraint("quantity_reserved <= quantity_on_hand", name="quantities_balance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # RESTRICT: deleting a variant that has stock records would destroy the
    # ledger explaining where that stock went.
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    quantity_on_hand: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    quantity_reserved: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    quantity_damaged: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    quantity_quarantined: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def available(self) -> int:
        """Stock a new reservation may draw on. The only definition of it.

        Damaged and quarantined units are not subtracted because they are not
        in ``quantity_on_hand`` to begin with - they live in their own
        columns. Moving stock into either bucket therefore reduces what is
        available by reducing on-hand itself.
        """
        return self.quantity_on_hand - self.quantity_reserved


class InventoryReservation(Base):
    """A temporary, expiring hold on stock for one cart.

    Reservations are not inventory transactions and never appear in the
    ledger: nothing physical moves when stock is held, released or expired.
    Terminal rows are kept forever as the auditable history of what was held
    and what became of it.
    """

    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint(_in_list(RESERVATION_STATUSES, "status"), name="status_valid"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # The expiry sweep asks exactly this question - "which rows on this
        # inventory row are still ACTIVE?" - on every reservation attempt.
        Index("ix_inventory_reservations_inventory_id_status", "inventory_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory.id", ondelete="RESTRICT"), nullable=False
    )
    # A direct foreign key, not a generic (reference_type, reference_id)
    # pair: a reservation in this phase is always held for a cart, and a
    # polymorphic column would trade a real constraint for a string.
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("carts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{RESERVATION_ACTIVE}'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InventoryTransaction(Base):
    """One immutable entry in the stock ledger: what physically moved.

    ``quantity`` is always a positive magnitude. Direction is carried by the
    buckets, not by the sign:

    * **inbound**  - ``from_bucket`` null, ``to_bucket`` set. Stock enters.
    * **outbound** - ``from_bucket`` set, ``to_bucket`` null. Stock leaves.
    * **transfer** - both set. Stock moves between buckets; the total in
      custody is unchanged.

    The types, and what each one means:

    ``PURCHASE``
        Outbound from ON_HAND. A customer bought it. Written when a
        reservation is converted, which is the only moment stock is
        permanently deducted. This is the only "sale" type - there is no
        SALE type.
    ``RESTOCK``
        Inbound to ON_HAND. The merchant received stock.
    ``RETURN``
        Inbound. A customer sent stock back. Phase 7 writes these; Phase 4
        defines the type and produces none.
    ``ADJUSTMENT``
        Inbound or outbound. A manual correction to a count.
    ``DISPOSAL``
        Outbound. Stock written off permanently.
    ``TRANSFER``
        Between buckets, e.g. ON_HAND to DAMAGED. Total physical stock is
        unchanged, but sellable stock falls.

    Rows are never updated or deleted by business logic. A correction is a
    new ADJUSTMENT row, never rewritten history.
    """

    __tablename__ = "inventory_transactions"
    __table_args__ = (
        CheckConstraint(_in_list(TRANSACTION_TYPES, "transaction_type"), name="type_valid"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            f"from_bucket IS NULL OR {_in_list(BUCKETS, 'from_bucket')}",
            name="from_bucket_valid",
        ),
        CheckConstraint(
            f"to_bucket IS NULL OR {_in_list(BUCKETS, 'to_bucket')}", name="to_bucket_valid"
        ),
        # A row with neither end set describes no movement at all, and would
        # silently break the reconciliation invariant.
        CheckConstraint(
            "from_bucket IS NOT NULL OR to_bucket IS NOT NULL", name="direction_present"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inventory.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    from_bucket: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_bucket: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Deliberately loose, unlike reservations.cart_id: a ledger entry may
    # point at a reservation now and an order or a return later, and those
    # tables do not all exist yet. Nothing joins on it.
    reference_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InventoryAdjustment(Base):
    """The human-facing half of a manual stock change: who, and why.

    The transaction it references is the machine-facing half: what moved.
    Both are written in the same transaction, so a stock change can never
    exist without an accountable actor and a stated reason.
    """

    __tablename__ = "inventory_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inventory.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # UNIQUE as well as indexed: an adjustment is the human half of exactly
    # one ledger entry, so two adjustments claiming the same transaction
    # would make "who changed this stock, and why" ambiguous. Added in
    # migration 0008, having been deferred when Phase 4 listed the columns
    # without it.
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    # NOT NULL and RESTRICT: unlike an audit log, which tolerates an unknown
    # actor, a manual stock change always has one and must keep naming them.
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
