"""Inventory business rules: stock levels, holds on them, and the ledger.

Three convictions run through this module.

First, **a reservation decision is never made on an unlocked read.** Unlike
the catalogue's uniqueness rules - a single INSERT that a unique index can
adjudicate on its own - reserving stock is a genuine check-then-act
sequence: read available, decide, write. Two transactions can both pass the
read before either writes. So the inventory row is locked with
``SELECT ... FOR UPDATE`` *before* available is computed and the lock is
held until commit.

Second, **reservations are not ledger entries.** Holding, releasing and
expiring stock move nothing physical, so they never produce an
``inventory_transactions`` row. The reservation table is its own lifecycle
history. This is what lets the ledger reconcile exactly against the quantity
columns.

Third, **the ledger is immutable.** Nothing here updates or deletes a
transaction row. A correction is a new ADJUSTMENT, never rewritten history.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    AppException,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    constraint_name,
)
from app.db.models.inventory import (
    BUCKET_COLUMNS,
    BUCKET_DAMAGED,
    BUCKET_ON_HAND,
    BUCKET_QUARANTINED,
    BUCKETS,
    RESERVATION_ACTIVE,
    RESERVATION_CONVERTED,
    RESERVATION_EXPIRED,
    RESERVATION_RELEASED,
    TRANSACTION_ADJUSTMENT,
    TRANSACTION_DISPOSAL,
    TRANSACTION_PURCHASE,
    TRANSACTION_RESTOCK,
    TRANSACTION_TRANSFER,
    TRANSACTION_TYPES,
    Inventory,
    InventoryReservation,
)
from app.db.models.cart import CART_ACTIVE
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.cart import repository as cart_repository
from app.modules.catalog import repository as catalog_repository
from app.modules.inventory import repository

# Not configurable. Five minutes is short enough that an abandoned checkout
# returns its stock quickly, and long enough to complete a payment. Making
# it a setting would invite per-environment drift in behaviour that every
# test and every customer expectation depends on.
RESERVATION_WINDOW = timedelta(minutes=5)

# Which moves are legal. Same shape as the catalogue's product and variant
# tables: one place to read the rules, one way to fail a forbidden move.
# All three non-ACTIVE states are terminal - a hold that ended, ended.
RESERVATION_TRANSITIONS: dict[str, frozenset[str]] = {
    RESERVATION_ACTIVE: frozenset(
        {RESERVATION_CONVERTED, RESERVATION_RELEASED, RESERVATION_EXPIRED}
    ),
    RESERVATION_CONVERTED: frozenset(),
    RESERVATION_RELEASED: frozenset(),
    RESERVATION_EXPIRED: frozenset(),
}

# Types an operator may write through the adjustments endpoint. PURCHASE is
# absent because only a reservation conversion may record a sale, and RETURN
# because Phase 7 owns returns.
ADJUSTABLE_TYPES = frozenset(
    {TRANSACTION_ADJUSTMENT, TRANSACTION_RESTOCK, TRANSACTION_DISPOSAL, TRANSACTION_TRANSFER}
)

_RESERVATION_REFERENCE = "reservation"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Provisioning and reads
# --------------------------------------------------------------------------


def provision_inventory(
    db: Session, *, variant_id: uuid.UUID, actor: User, client: ClientInfo
) -> Inventory:
    """Create the single zero-quantity inventory row for a variant.

    Zero rather than an opening balance: stock that arrived has to arrive
    through the ledger, or the reconciliation invariant is broken from the
    first row. Deliberately not hooked into Phase 3's variant creation -
    that is locked code, and provisioning is a merchandising step.
    """
    variant = catalog_repository.get_variant(db, variant_id)
    if variant is None:
        raise NotFoundError("Variant not found.", code="VARIANT_NOT_FOUND")

    try:
        inventory = repository.add_inventory(db, variant_id=variant.id)
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.INVENTORY_PROVISIONED,
        actor_user_id=actor.id,
        client=client,
        details={"variant_id": str(variant.id), "inventory_id": str(inventory.id)},
    )
    db.commit()
    db.refresh(inventory)
    return inventory


def require_inventory_for_variant(db: Session, variant_id: uuid.UUID) -> Inventory:
    inventory = repository.get_inventory_by_variant(db, variant_id)
    if inventory is None:
        raise NotFoundError(
            "No inventory has been provisioned for that variant.", code="INVENTORY_NOT_FOUND"
        )
    return inventory


def build_inventory_view(inventory: Inventory) -> dict[str, object]:
    return {
        "id": inventory.id,
        "variant_id": inventory.variant_id,
        "quantity_on_hand": inventory.quantity_on_hand,
        "quantity_reserved": inventory.quantity_reserved,
        "quantity_damaged": inventory.quantity_damaged,
        "quantity_quarantined": inventory.quantity_quarantined,
        # The one and only definition of available lives on the model.
        "available": inventory.available,
        "created_at": inventory.created_at,
        "updated_at": inventory.updated_at,
    }


def list_transactions(db: Session, variant_id: uuid.UUID):
    return repository.list_transactions(db, require_inventory_for_variant(db, variant_id).id)


def list_reservations(db: Session, variant_id: uuid.UUID):
    return repository.list_reservations(db, require_inventory_for_variant(db, variant_id).id)


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------


def reserve(
    db: Session,
    *,
    variant_id: uuid.UUID,
    cart_id: uuid.UUID,
    quantity: int,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Hold stock for a cart, as one self-contained transaction.

    A thin wrapper over :func:`reserve_locked`, which does the work. The
    split exists because checkout has to hold *several* reservations and an
    order insert inside one transaction, and a function that commits cannot
    take part in somebody else's unit of work.

    Everything about this function's behaviour is unchanged: same
    arguments, same return, same error codes, same all-or-nothing outcome.
    Transaction ownership simply moved out of the body and into these lines.
    """
    try:
        reservation = reserve_locked(
            db,
            variant_id=variant_id,
            cart_id=cart_id,
            quantity=quantity,
            actor=actor,
            client=client,
        )
        db.commit()
    except Exception:
        # Including the ordinary rejections. Half of an atomic operation is
        # worse than none, and the inner half deliberately leaves that
        # decision to whoever owns the transaction - here, that is us.
        db.rollback()
        raise

    db.refresh(reservation)
    return reservation


def reserve_locked(
    db: Session,
    *,
    variant_id: uuid.UUID,
    cart_id: uuid.UUID,
    quantity: int,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Hold stock for a cart **inside the caller's transaction**.

    Neither commits nor rolls back. It raises, leaving the session as it
    found it, so a caller that has already built other work - an order row,
    three other reservations - still has that work and can choose what to
    do with it. The caller must commit, or roll back, or all of it is lost.

    The row lock is taken here and, because nothing commits, is held until
    the caller's transaction ends. That is what lets checkout reserve
    several lines with no window between them in which another customer
    could take the stock.
    """
    if quantity < 1:
        raise BusinessRuleError(
            "A reservation must be for at least one unit.",
            code="INVALID_RESERVATION_QUANTITY",
        )

    # Checked before anything is locked, because a reservation is held *for*
    # a cart and a hold against a cart that does not exist means nothing.
    # Phase 4 took this id on trust, so an unknown one reached PostgreSQL as
    # an unmapped foreign-key violation - a 500 for an ordinary bad request.
    cart = cart_repository.get_cart(db, cart_id)
    if cart is None:
        raise NotFoundError("Cart not found.", code="CART_NOT_FOUND")
    if cart.status != CART_ACTIVE:
        raise BusinessRuleError(
            "That cart is no longer active and cannot hold stock.",
            code="CART_NOT_ACTIVE",
        )

    inventory = repository.lock_inventory_by_variant(db, variant_id)
    if inventory is None:
        raise NotFoundError(
            "No inventory has been provisioned for that variant.", code="INVENTORY_NOT_FOUND"
        )

    # Opportunistic expiry, under the lock and before available is computed.
    #
    # Nothing schedules expiry in Phase 4 - workers are Phase 8 - so without
    # this a reservation whose window elapsed would sit ACTIVE forever and
    # strand its stock permanently. Sweeping here reclaims that stock at
    # exactly the moment somebody needs it.
    #
    # The distinction that matters: a read-only display of available stock
    # may lag, by as much as the age of the oldest past-due hold. A
    # reservation *decision* never does, because the sweep and the decision
    # happen under the same lock in the same transaction.
    _expire_due(db, inventory, client=client)

    if quantity > inventory.available:
        # Raised, not rolled back. The sweep above is genuine work the
        # transaction owner may want to keep, and discarding their pending
        # order row here would be exactly the behaviour this split removed.
        raise BusinessRuleError(
            "There is not enough available stock to reserve that quantity.",
            code="INSUFFICIENT_INVENTORY",
        )

    inventory.quantity_reserved += quantity
    try:
        reservation = repository.add_reservation(
            db,
            inventory_id=inventory.id,
            cart_id=cart_id,
            quantity=quantity,
            expires_at=_now() + RESERVATION_WINDOW,
        )
    except IntegrityError as error:
        # The second layer under the cart check above: were the cart deleted
        # in the window between them, the foreign key fires and is
        # translated rather than escaping unmapped as a 500.
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.INVENTORY_RESERVED,
        actor_user_id=actor.id if actor else None,
        client=client,
        details={
            "inventory_id": str(inventory.id),
            "reservation_id": str(reservation.id),
            "cart_id": str(cart_id),
            "quantity": quantity,
        },
    )
    return reservation


def convert_reservation(
    db: Session,
    *,
    reservation_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Turn a hold into a sale, as one self-contained transaction.

    The committing wrapper over :func:`convert_reservation_locked`, split
    for the same reason as ``reserve``: confirming an order converts every
    one of its reservations, and either all of them become sales or none
    of them do.
    """
    return _own_the_transaction(
        db,
        convert_reservation_locked,
        reservation_id=reservation_id,
        actor=actor,
        client=client,
    )


def convert_reservation_locked(
    db: Session,
    *,
    reservation_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Turn a hold into a sale **inside the caller's transaction**.

    This is the only place stock is permanently deducted. Creating a
    reservation deliberately does not touch ``quantity_on_hand``: until
    somebody has paid, the units are still the merchant's.

    Neither commits nor rolls back; the caller owns both.
    """
    reservation, inventory = _lock_for_transition(db, reservation_id, RESERVATION_CONVERTED)

    inventory.quantity_reserved -= reservation.quantity
    inventory.quantity_on_hand -= reservation.quantity
    reservation.status = RESERVATION_CONVERTED

    repository.add_transaction(
        db,
        inventory_id=inventory.id,
        transaction_type=TRANSACTION_PURCHASE,
        quantity=reservation.quantity,
        from_bucket=BUCKET_ON_HAND,
        to_bucket=None,
        reference_type=_RESERVATION_REFERENCE,
        reference_id=reservation.id,
    )

    audit.record_event(
        db,
        event_type=audit.INVENTORY_CONVERTED,
        actor_user_id=actor.id if actor else None,
        client=client,
        details={
            "inventory_id": str(inventory.id),
            "reservation_id": str(reservation.id),
            "quantity": reservation.quantity,
        },
    )
    return reservation


def release_reservation(
    db: Session,
    *,
    reservation_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Give a hold back, as one self-contained transaction.

    The committing wrapper over :func:`release_reservation_locked`.
    Cancelling an order releases every line it held, and a partially
    released order would leave stock stranded with nothing to notice.
    """
    return _own_the_transaction(
        db,
        release_reservation_locked,
        reservation_id=reservation_id,
        actor=actor,
        client=client,
    )


def release_reservation_locked(
    db: Session,
    *,
    reservation_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> InventoryReservation:
    """Give a hold back **inside the caller's transaction**.

    No ledger entry: nothing physical moved. The status and the counter
    always change together - either both or neither.

    Neither commits nor rolls back; the caller owns both.
    """
    reservation, inventory = _lock_for_transition(db, reservation_id, RESERVATION_RELEASED)

    inventory.quantity_reserved -= reservation.quantity
    reservation.status = RESERVATION_RELEASED

    audit.record_event(
        db,
        event_type=audit.INVENTORY_RELEASED,
        actor_user_id=actor.id if actor else None,
        client=client,
        details={
            "inventory_id": str(inventory.id),
            "reservation_id": str(reservation.id),
            "quantity": reservation.quantity,
        },
    )
    return reservation


def _own_the_transaction(db: Session, work, **arguments) -> InventoryReservation:
    """Run one composable reservation step as a transaction of its own.

    The two wrappers above are identical apart from which inner function
    they call, and the property that matters - commit on success, roll back
    on *any* failure - is worth stating once rather than twice.
    """
    try:
        reservation = work(db, **arguments)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(reservation)
    return reservation


def expire_due_reservations(
    db: Session, inventory_id: uuid.UUID, *, client: ClientInfo | None = None
) -> int:
    """Expire every past-due hold on one inventory row, and say how many.

    The reservation path calls the same sweep inline. This entry point
    exists so Phase 8 can drive expiry proactively rather than only as a
    side effect of the next customer to want the stock.
    """
    inventory = repository.lock_inventory(db, inventory_id)
    if inventory is None:
        raise NotFoundError("Inventory not found.", code="INVENTORY_NOT_FOUND")

    expired = _expire_due(db, inventory, client=client)
    db.commit()
    return expired


def _expire_due(db: Session, inventory: Inventory, *, client: ClientInfo | None) -> int:
    """The sweep itself. The caller must already hold the row lock."""
    due = repository.list_due_active_reservations(db, inventory.id, _now())

    for reservation in due:
        _require_transition(reservation.status, RESERVATION_EXPIRED)
        reservation.status = RESERVATION_EXPIRED
        inventory.quantity_reserved -= reservation.quantity
        audit.record_event(
            db,
            event_type=audit.INVENTORY_EXPIRED,
            client=client,
            details={
                "inventory_id": str(inventory.id),
                "reservation_id": str(reservation.id),
                "quantity": reservation.quantity,
            },
        )

    if due:
        db.flush()
    return len(due)


def _lock_for_transition(
    db: Session, reservation_id: uuid.UUID, target: str
) -> tuple[InventoryReservation, Inventory]:
    """Find a reservation, lock its inventory row, then re-check its state.

    The order matters. The first read is unlocked and only tells us which
    row to lock; the state that decides whether the transition is legal is
    re-read afterwards, under the lock, because every write to a reservation
    happens while holding it. Checking before locking would let two
    concurrent conversions both see ACTIVE.
    """
    reservation = repository.get_reservation(db, reservation_id)
    if reservation is None:
        raise NotFoundError("Reservation not found.", code="RESERVATION_NOT_FOUND")

    inventory = repository.lock_inventory(db, reservation.inventory_id)
    if inventory is None:
        raise NotFoundError("Inventory not found.", code="INVENTORY_NOT_FOUND")

    db.refresh(reservation)
    _require_transition(reservation.status, target)
    return reservation, inventory


def _require_transition(current: str, target: str) -> None:
    if target not in RESERVATION_TRANSITIONS.get(current, frozenset()):
        raise BusinessRuleError(
            f"A reservation cannot move from {current} to {target}.",
            code="INVALID_RESERVATION_STATE",
        )


# --------------------------------------------------------------------------
# Adjustments
# --------------------------------------------------------------------------


def adjust_inventory(
    db: Session,
    *,
    variant_id: uuid.UUID,
    transaction_type: str,
    quantity: int,
    from_bucket: str | None,
    to_bucket: str | None,
    reason: str,
    actor: User,
    client: ClientInfo,
) -> Inventory:
    """Move stock, record what moved, and record who moved it and why.

    The adjustment row is the human-facing half (actor, reason); the
    transaction is the ledger half (what moved). Both are written in one
    transaction, so a stock change can never exist unaccounted for.

    Bucket moves use this same path with both buckets supplied, producing a
    TRANSFER. Because the buckets are disjoint, moving three units from
    ON_HAND to DAMAGED really does decrement on-hand and increment damaged -
    total physical stock is unchanged, sellable stock falls by three.
    """
    _validate_adjustment(transaction_type, quantity, from_bucket, to_bucket, reason)

    inventory = repository.lock_inventory_by_variant(db, variant_id)
    if inventory is None:
        db.rollback()
        raise NotFoundError(
            "No inventory has been provisioned for that variant.", code="INVENTORY_NOT_FOUND"
        )

    try:
        # The whole mutation sits inside the try, not just a closing flush:
        # the repository helpers flush as they go, so the constraint can
        # fire on any of them. The balance and non-negative constraints are
        # what decide whether the result is legal - not a Python pre-check,
        # which would leave a window between the check and the write.
        if from_bucket is not None:
            _apply(inventory, from_bucket, -quantity)
        if to_bucket is not None:
            _apply(inventory, to_bucket, quantity)

        transaction = repository.add_transaction(
            db,
            inventory_id=inventory.id,
            transaction_type=transaction_type,
            quantity=quantity,
            from_bucket=from_bucket,
            to_bucket=to_bucket,
        )
        repository.add_adjustment(
            db,
            inventory_id=inventory.id,
            transaction_id=transaction.id,
            actor_user_id=actor.id,
            reason=reason,
        )
        audit.record_event(
            db,
            event_type=audit.INVENTORY_ADJUSTED,
            actor_user_id=actor.id,
            client=client,
            details={
                "inventory_id": str(inventory.id),
                "transaction_type": transaction_type,
                "quantity": quantity,
                "from_bucket": from_bucket,
                "to_bucket": to_bucket,
            },
        )
        db.flush()
    except IntegrityError as error:
        # Rolls back the ledger row and the adjustment row along with the
        # quantity change: a refused adjustment leaves no trace at all.
        db.rollback()
        raise _translate(error) from None

    db.commit()
    db.refresh(inventory)
    return inventory


def _apply(inventory: Inventory, bucket: str, delta: int) -> None:
    column = BUCKET_COLUMNS[bucket]
    setattr(inventory, column, getattr(inventory, column) + delta)


def _validate_adjustment(
    transaction_type: str, quantity: int, from_bucket: str | None, to_bucket: str | None, reason: str
) -> None:
    if quantity <= 0:
        # Quantity is a magnitude; the buckets carry the direction. A zero
        # adjustment describes no movement and would pollute the ledger.
        raise _invalid("An adjustment quantity must be a positive number of units.")

    if not reason or not reason.strip():
        raise _invalid("An adjustment must state a reason.")

    if transaction_type not in ADJUSTABLE_TYPES:
        raise _invalid(
            f"{transaction_type} cannot be recorded through an adjustment. "
            f"Use one of: {', '.join(sorted(ADJUSTABLE_TYPES))}."
        )

    for bucket in (from_bucket, to_bucket):
        if bucket is not None and bucket not in BUCKETS:
            raise _invalid(f"{bucket} is not a stock bucket.")

    if from_bucket is None and to_bucket is None:
        raise _invalid("An adjustment must say which bucket stock moves from, to, or between.")

    if from_bucket is not None and from_bucket == to_bucket:
        raise _invalid("An adjustment cannot move stock from a bucket into itself.")

    both = from_bucket is not None and to_bucket is not None
    if transaction_type == TRANSACTION_TRANSFER and not both:
        raise _invalid("A TRANSFER must name both the bucket stock leaves and the one it enters.")
    if transaction_type != TRANSACTION_TRANSFER and both:
        raise _invalid("Only a TRANSFER may name both buckets.")
    if transaction_type == TRANSACTION_RESTOCK and from_bucket is not None:
        raise _invalid("A RESTOCK is inbound: it names only the bucket stock enters.")
    if transaction_type == TRANSACTION_DISPOSAL and to_bucket is not None:
        raise _invalid("A DISPOSAL is outbound: it names only the bucket stock leaves.")


def _invalid(message: str) -> BusinessRuleError:
    raise BusinessRuleError(message, code="INVALID_INVENTORY_ADJUSTMENT")


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def reconcile(db: Session, inventory: Inventory) -> dict[str, tuple[int, int]]:
    """Per bucket, what the ledger says versus what the column says.

    The invariant: for every bucket, the sum of inbound quantities minus the
    sum of outbound quantities equals the current column value. Reservation
    events are kept out of the ledger precisely so this holds exactly.
    """
    from_ledger = dict.fromkeys(BUCKETS, 0)
    for row in repository.bucket_movements(db, inventory.id):
        from_ledger[row.bucket] += row.quantity

    return {
        bucket: (from_ledger[bucket], getattr(inventory, BUCKET_COLUMNS[bucket]))
        for bucket in BUCKETS
    }


# --------------------------------------------------------------------------
# Constraint translation
# --------------------------------------------------------------------------

# Same pattern as the catalogue's, with one addition: the exception class
# travels with the code, because a duplicate inventory row is a conflict
# (409) while an adjustment that would oversell reserved stock is a rejected
# business rule (422).
_CONFLICTS: dict[str, tuple[type[AppException], str, str]] = {
    "uq_inventory_variant_id": (
        ConflictError,
        "INVENTORY_ALREADY_EXISTS",
        "That variant already has an inventory row.",
    ),
    "ck_inventory_quantities_balance": (
        BusinessRuleError,
        "INVALID_INVENTORY_ADJUSTMENT",
        "That change would leave less stock on hand than is already reserved.",
    ),
    "ck_inventory_quantity_on_hand_non_negative": (
        BusinessRuleError,
        "INVALID_INVENTORY_ADJUSTMENT",
        "That change would take more stock than is on hand.",
    ),
    "ck_inventory_quantity_reserved_non_negative": (
        BusinessRuleError,
        "INVALID_INVENTORY_ADJUSTMENT",
        "That change would reserve a negative quantity.",
    ),
    "ck_inventory_quantity_damaged_non_negative": (
        BusinessRuleError,
        "INVALID_INVENTORY_ADJUSTMENT",
        "That change would take more stock than is recorded as damaged.",
    ),
    "ck_inventory_quantity_quarantined_non_negative": (
        BusinessRuleError,
        "INVALID_INVENTORY_ADJUSTMENT",
        "That change would take more stock than is recorded as quarantined.",
    ),
    # The backstop under reserve_locked's own cart check. Mapped so that a
    # cart vanishing inside the check-then-act window is reported as the
    # ordinary 404 it is, rather than escaping as an unmapped 500.
    "fk_inventory_reservations_cart_id_carts": (
        NotFoundError,
        "CART_NOT_FOUND",
        "Cart not found.",
    ),
}


def _translate(error: IntegrityError) -> Exception:
    conflict = _CONFLICTS.get(constraint_name(error))
    if conflict is None:
        # An unmapped constraint surfaces as a 500 rather than a plausible
        # but wrong 409, which is what makes the mapping above worth keeping
        # honest.
        return error
    exception_class, code, message = conflict
    return exception_class(message, code=code)


__all__ = [
    "ADJUSTABLE_TYPES",
    "BUCKETS",
    "BUCKET_DAMAGED",
    "BUCKET_ON_HAND",
    "BUCKET_QUARANTINED",
    "RESERVATION_TRANSITIONS",
    "RESERVATION_WINDOW",
    "TRANSACTION_TYPES",
    "adjust_inventory",
    "build_inventory_view",
    "convert_reservation",
    "convert_reservation_locked",
    "expire_due_reservations",
    "list_reservations",
    "list_transactions",
    "provision_inventory",
    "reconcile",
    "release_reservation",
    "release_reservation_locked",
    "require_inventory_for_variant",
    "reserve",
    "reserve_locked",
]
