"""Every order database query, including the two locks checkout depends on.

Two things here are load-bearing rather than incidental.

``lock_order`` is what makes cancelling and confirming safe: both are
check-then-act on ``status``, so the row is locked before the status is
read and held until the caller commits.

``active_reservations_for_cart`` returns its rows **ordered by variant id**.
That is not cosmetic. Confirming or cancelling an order takes one inventory
lock per line, and a fixed global acquisition order is what makes a deadlock
between two such operations impossible - the same rule checkout follows when
it reserves.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.db.models.catalog import ProductVariant
from app.db.models.inventory import RESERVATION_ACTIVE, Inventory, InventoryReservation
from app.db.models.order import Order, OrderItem

# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def get_order(db: Session, order_id: uuid.UUID) -> Order | None:
    return db.get(Order, order_id)


def lock_order(db: Session, order_id: uuid.UUID) -> Order | None:
    """Take the row lock that makes a status transition safe.

    Held until the caller's transaction ends. Two concurrent cancellations
    serialize here, so the second reads CANCELLED rather than ACTIVE and is
    refused by the state machine instead of releasing the stock twice.
    """
    return db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    ).scalar_one_or_none()


def get_order_by_cart(db: Session, cart_id: uuid.UUID) -> Order | None:
    return db.execute(select(Order).where(Order.cart_id == cart_id)).scalar_one_or_none()


def latest_order_for_user(db: Session, user_id: uuid.UUID) -> Order | None:
    """The most recently placed order belonging to one customer.

    Used only by checkout's replay path, and only when the customer has no
    ACTIVE cart at all - a state that, because checkout is the only writer
    of CONVERTED and cancellation hands the lines back to a fresh cart, can
    only mean "your last action was a completed checkout".
    """
    return db.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.placed_at.desc(), Order.id)
        .limit(1)
    ).scalar_one_or_none()


def list_for_user(db: Session, user_id: uuid.UUID, *, limit: int, offset: int) -> list[Order]:
    return list(
        db.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.placed_at.desc(), Order.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def list_all(db: Session, *, limit: int, offset: int, status: str | None = None) -> list[Order]:
    statement = select(Order)
    if status is not None:
        statement = statement.where(Order.status == status)
    return list(
        db.execute(
            statement.order_by(Order.placed_at.desc(), Order.id).limit(limit).offset(offset)
        ).scalars()
    )


def reference_exists(db: Session, reference: str) -> bool:
    return (
        db.execute(select(Order.id).where(Order.reference == reference).limit(1)).first()
        is not None
    )


def add_order(db: Session, **fields) -> Order:
    order = Order(**fields)
    db.add(order)
    db.flush()
    return order


# --------------------------------------------------------------------------
# Order items
# --------------------------------------------------------------------------


def add_item(db: Session, **fields) -> OrderItem:
    item = OrderItem(**fields)
    db.add(item)
    db.flush()
    return item


def list_items(db: Session, order_id: uuid.UUID) -> list[OrderItem]:
    return list(
        db.execute(
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.created_at, OrderItem.id)
        ).scalars()
    )


def list_items_for_orders(db: Session, order_ids: Sequence[uuid.UUID]) -> list[OrderItem]:
    """Every line of several orders in one query, for list responses."""
    if not order_ids:
        return []
    return list(
        db.execute(
            select(OrderItem)
            .where(OrderItem.order_id.in_(order_ids))
            .order_by(OrderItem.order_id, OrderItem.created_at, OrderItem.id)
        ).scalars()
    )


# --------------------------------------------------------------------------
# The link to Phase 4's reservations
# --------------------------------------------------------------------------


def active_reservations_for_cart(db: Session, cart_id: uuid.UUID) -> list[Row]:
    """This order's live stock holds, in a deterministic lock order.

    An order finds its reservations through its cart, reusing Phase 4's
    direct ``inventory_reservations.cart_id`` foreign key rather than
    duplicating it as an ``order_id``. The cart can never be deleted -
    that key is RESTRICT - so the route is permanent.

    Ordered by variant id so that confirming and cancelling acquire their
    inventory locks in the same global sequence as checkout does.
    """
    return list(
        db.execute(
            select(InventoryReservation.id, ProductVariant.id.label("variant_id"))
            .join(Inventory, Inventory.id == InventoryReservation.inventory_id)
            .join(ProductVariant, ProductVariant.id == Inventory.variant_id)
            .where(
                InventoryReservation.cart_id == cart_id,
                InventoryReservation.status == RESERVATION_ACTIVE,
            )
            .order_by(ProductVariant.id)
        )
    )


# --------------------------------------------------------------------------
# Catalogue dependents, for the Phase 3 deletion guard
# --------------------------------------------------------------------------


def count_ordered_variants_of_product(db: Session, product_id: uuid.UUID) -> int:
    """Variants of one product that appear on at least one order.

    The guarantee underneath is ``order_items.variant_id``, which is
    RESTRICT, so PostgreSQL refuses the deletion even if this guard were
    removed. What the count adds is the *answer the operator gets*: "this
    has 2 ordered variants" rather than an opaque foreign-key error.
    """
    return db.execute(
        select(func.count(func.distinct(OrderItem.variant_id)))
        .select_from(OrderItem)
        .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
        .where(ProductVariant.product_id == product_id)
    ).scalar_one()
