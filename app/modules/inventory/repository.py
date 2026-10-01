"""Every inventory database query, including the locking mechanics.

The two ``lock_*`` functions are the only place ``FOR UPDATE`` appears in
this phase, and they exist because reservation is a genuine check-then-act
sequence: read available, decide, write. Two transactions can both pass the
read before either writes, so the row has to be locked *before* available is
computed and held until commit.
"""

import uuid
from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.db.models.inventory import (
    RESERVATION_ACTIVE,
    Inventory,
    InventoryAdjustment,
    InventoryReservation,
    InventoryTransaction,
)

# --------------------------------------------------------------------------
# Inventory rows
# --------------------------------------------------------------------------


def get_inventory_by_variant(db: Session, variant_id: uuid.UUID) -> Inventory | None:
    """An unlocked read, for display only. Never for a reservation decision."""
    return db.execute(
        select(Inventory).where(Inventory.variant_id == variant_id)
    ).scalar_one_or_none()


def lock_inventory_by_variant(db: Session, variant_id: uuid.UUID) -> Inventory | None:
    """Take the row lock that makes a reservation decision safe.

    Held until the caller's transaction ends. Everything that changes a
    quantity column goes through this or ``lock_inventory``.
    """
    return db.execute(
        select(Inventory).where(Inventory.variant_id == variant_id).with_for_update()
    ).scalar_one_or_none()


def lock_inventory(db: Session, inventory_id: uuid.UUID) -> Inventory | None:
    return db.execute(
        select(Inventory).where(Inventory.id == inventory_id).with_for_update()
    ).scalar_one_or_none()


def add_inventory(db: Session, *, variant_id: uuid.UUID) -> Inventory:
    inventory = Inventory(variant_id=variant_id)
    db.add(inventory)
    db.flush()
    return inventory


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------


def get_reservation(db: Session, reservation_id: uuid.UUID) -> InventoryReservation | None:
    return db.get(InventoryReservation, reservation_id)


def add_reservation(
    db: Session,
    *,
    inventory_id: uuid.UUID,
    cart_id: uuid.UUID,
    quantity: int,
    expires_at: datetime,
) -> InventoryReservation:
    reservation = InventoryReservation(
        inventory_id=inventory_id, cart_id=cart_id, quantity=quantity, expires_at=expires_at
    )
    db.add(reservation)
    db.flush()
    return reservation


def list_due_active_reservations(
    db: Session, inventory_id: uuid.UUID, now: datetime
) -> list[InventoryReservation]:
    """Past-due holds on one inventory row.

    Served by the (inventory_id, status) index. The caller must already hold
    the lock on that inventory row.
    """
    return list(
        db.execute(
            select(InventoryReservation).where(
                InventoryReservation.inventory_id == inventory_id,
                InventoryReservation.status == RESERVATION_ACTIVE,
                InventoryReservation.expires_at <= now,
            )
        ).scalars()
    )


def list_reservations(db: Session, inventory_id: uuid.UUID) -> list[InventoryReservation]:
    return list(
        db.execute(
            select(InventoryReservation)
            .where(InventoryReservation.inventory_id == inventory_id)
            .order_by(InventoryReservation.created_at.desc(), InventoryReservation.id)
        ).scalars()
    )


# --------------------------------------------------------------------------
# Ledger and adjustments
# --------------------------------------------------------------------------


def add_transaction(
    db: Session,
    *,
    inventory_id: uuid.UUID,
    transaction_type: str,
    quantity: int,
    from_bucket: str | None,
    to_bucket: str | None,
    reference_type: str | None = None,
    reference_id: uuid.UUID | None = None,
) -> InventoryTransaction:
    transaction = InventoryTransaction(
        inventory_id=inventory_id,
        transaction_type=transaction_type,
        quantity=quantity,
        from_bucket=from_bucket,
        to_bucket=to_bucket,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    db.add(transaction)
    db.flush()
    return transaction


def list_transactions(db: Session, inventory_id: uuid.UUID) -> list[InventoryTransaction]:
    return list(
        db.execute(
            select(InventoryTransaction)
            .where(InventoryTransaction.inventory_id == inventory_id)
            .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
        ).scalars()
    )


def add_adjustment(
    db: Session,
    *,
    inventory_id: uuid.UUID,
    transaction_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    reason: str,
) -> InventoryAdjustment:
    adjustment = InventoryAdjustment(
        inventory_id=inventory_id,
        transaction_id=transaction_id,
        actor_user_id=actor_user_id,
        reason=reason,
    )
    db.add(adjustment)
    db.flush()
    return adjustment


def bucket_movements(db: Session, inventory_id: uuid.UUID) -> list[Row]:
    """Net movement per bucket, straight from the ledger.

    Used to verify the reconciliation invariant: for each bucket, inbound
    minus outbound equals that bucket's current column value. A TRANSFER
    counts as an outbound from one bucket and an inbound to the other,
    which is exactly why the buckets have to be disjoint.
    """
    inbound = select(
        InventoryTransaction.to_bucket.label("bucket"),
        func.sum(InventoryTransaction.quantity).label("quantity"),
        func.count().label("entries"),
    ).where(
        InventoryTransaction.inventory_id == inventory_id,
        InventoryTransaction.to_bucket.is_not(None),
    ).group_by(InventoryTransaction.to_bucket)

    outbound = select(
        InventoryTransaction.from_bucket.label("bucket"),
        (-func.sum(InventoryTransaction.quantity)).label("quantity"),
        func.count().label("entries"),
    ).where(
        InventoryTransaction.inventory_id == inventory_id,
        InventoryTransaction.from_bucket.is_not(None),
    ).group_by(InventoryTransaction.from_bucket)

    return list(db.execute(inbound.union_all(outbound)))


# --------------------------------------------------------------------------
# Catalogue dependents, for the Phase 3 deletion guard
# --------------------------------------------------------------------------


def count_stocked_variants_of_product(db: Session, product_id: uuid.UUID) -> int:
    """Variants of one product whose inventory is not safe to discard.

    "Not safe" means any non-zero quantity, any ledger history, or any
    reservation still alive. A provisioned-but-untouched variant does not
    count: an all-zero row with no history records nothing worth keeping.
    """
    from app.db.models.catalog import ProductVariant

    held = (
        select(func.count())
        .select_from(Inventory)
        .join(ProductVariant, ProductVariant.id == Inventory.variant_id)
        .where(
            ProductVariant.product_id == product_id,
            (
                (Inventory.quantity_on_hand > 0)
                | (Inventory.quantity_reserved > 0)
                | (Inventory.quantity_damaged > 0)
                | (Inventory.quantity_quarantined > 0)
                | select(InventoryTransaction.id)
                .where(InventoryTransaction.inventory_id == Inventory.id)
                .exists()
                | select(InventoryReservation.id)
                .where(
                    InventoryReservation.inventory_id == Inventory.id,
                    InventoryReservation.status == RESERVATION_ACTIVE,
                )
                .exists()
            ),
        )
    )
    return db.execute(held).scalar_one()
