"""Cart business rules: ownership, item arithmetic, availability.

Two convictions run through this module.

First, **ownership is the authorization**. Every function here takes the
authenticated ``User`` and resolves the cart from it. No function accepts a
cart id, and no route exposes one, so there is no identifier a caller could
change to reach somebody else's cart. This is also why cart endpoints carry
no permission code: Phase 2 locked "new users start with zero roles", so a
``cart.*`` code would refuse every newly registered customer access to their
own cart until an administrator intervened.

Second, **an item already in the cart is never silently removed**. When a
variant stops being sellable the line stays and is reported unavailable with
a reason. Deleting it would quietly discard something the customer chose;
showing it as buyable would be untrue. Phase 5 checkout rejects a cart that
contains unavailable lines explicitly.
"""

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessRuleError, NotFoundError, constraint_name
from app.db.models.cart import Cart, CartItem
from app.db.models.catalog import PRODUCT_ACTIVE, VARIANT_ACTIVE
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.cart import repository
from app.modules.catalog import repository as catalog_repository

# Why a line cannot be bought. Reported to the client rather than being
# inferred there, so the storefront and Phase 5 checkout agree on the reason.
UNAVAILABLE_VARIANT_NOT_ACTIVE = "VARIANT_NOT_ACTIVE"
UNAVAILABLE_PRODUCT_NOT_ACTIVE = "PRODUCT_NOT_ACTIVE"

_ONE_ACTIVE_CART_INDEX = "uq_carts_one_active_per_user"
_ONE_LINE_PER_VARIANT_CONSTRAINT = "uq_cart_items_cart_id_variant_id"


# --------------------------------------------------------------------------
# The cart itself
# --------------------------------------------------------------------------


def get_or_create_active_cart(db: Session, user: User) -> Cart:
    """The caller's live cart, created on first use.

    Two concurrent first requests both find nothing and both try to insert.
    The partial unique index rejects one of them; that loser re-reads and
    returns the winner's cart. Handled inline rather than through a retry
    helper because it happens in exactly one place and happens once per
    customer, ever.
    """
    cart = repository.get_active_cart(db, user.id)
    if cart is not None:
        return cart

    try:
        cart = repository.add_cart(db, user_id=user.id)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if constraint_name(error) != _ONE_ACTIVE_CART_INDEX:
            raise
        existing = repository.get_active_cart(db, user.id)
        if existing is None:
            # The index fired but no cart is visible, which means something
            # other than the race we handle here. Do not paper over it.
            raise
        return existing

    db.refresh(cart)
    return cart


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


def add_item(
    db: Session,
    *,
    user: User,
    variant_id: uuid.UUID,
    quantity: int,
    client: ClientInfo,
) -> Cart:
    """Add a variant, or raise the quantity of the line already holding it.

    Two concurrent adds of the same variant both read "no such line yet" and
    both insert; the unique constraint rejects one. Because the contract
    says a duplicate add *increments*, the loser re-reads and increments
    rather than reporting a conflict - the caller's intent is still
    satisfiable. Handled inline, exactly like the get-or-create race above,
    and deliberately attempted only once: a second failure is not this race.
    """
    _require_positive(quantity)
    cart = get_or_create_active_cart(db, user)
    variant = _require_sellable_variant(db, variant_id)
    # Captured as plain values: a rollback below expires both objects.
    cart_id, resolved_variant_id = cart.id, variant.id

    try:
        _write_item(
            db, user=user, client=client, cart_id=cart_id,
            variant_id=resolved_variant_id, quantity=quantity,
        )
    except IntegrityError as error:
        db.rollback()
        if constraint_name(error) != _ONE_LINE_PER_VARIANT_CONSTRAINT:
            raise
        _write_item(
            db, user=user, client=client, cart_id=cart_id,
            variant_id=resolved_variant_id, quantity=quantity,
        )

    return cart


def _write_item(
    db: Session,
    *,
    user: User,
    client: ClientInfo,
    cart_id: uuid.UUID,
    variant_id: uuid.UUID,
    quantity: int,
) -> None:
    """Insert or increment the line, audit it, and commit - or raise.

    One unit of work, so the retry above re-runs all of it. The repository
    helper flushes internally, so the insert can raise from inside here
    rather than only at the commit.
    """
    existing = repository.get_item_by_variant(db, cart_id=cart_id, variant_id=variant_id)
    if existing is not None:
        existing.quantity += quantity
        item = existing
    else:
        item = repository.add_item(
            db, cart_id=cart_id, variant_id=variant_id, quantity=quantity
        )

    audit.record_event(
        db,
        event_type=audit.CART_ITEM_ADDED,
        actor_user_id=user.id,
        client=client,
        details={
            "cart_id": str(cart_id),
            "variant_id": str(variant_id),
            "quantity": item.quantity,
        },
    )
    db.commit()


def update_item_quantity(
    db: Session, *, user: User, item_id: uuid.UUID, quantity: int, client: ClientInfo
) -> Cart:
    """Set a line's quantity. Zero is the defined way to remove it."""
    if quantity < 0:
        raise BusinessRuleError(
            "A cart item quantity cannot be negative.", code="INVALID_CART_ITEM_QUANTITY"
        )

    cart = get_or_create_active_cart(db, user)
    item = _require_item_of_cart(db, item_id, cart.id)

    if quantity == 0:
        return _remove(db, cart=cart, item=item, user=user, client=client)

    item.quantity = quantity
    audit.record_event(
        db,
        event_type=audit.CART_ITEM_UPDATED,
        actor_user_id=user.id,
        client=client,
        details={"cart_id": str(cart.id), "item_id": str(item.id), "quantity": quantity},
    )
    db.commit()
    return cart


def remove_item(db: Session, *, user: User, item_id: uuid.UUID, client: ClientInfo) -> None:
    cart = get_or_create_active_cart(db, user)
    item = _require_item_of_cart(db, item_id, cart.id)
    _remove(db, cart=cart, item=item, user=user, client=client)


def clear_cart(db: Session, *, user: User, client: ClientInfo) -> None:
    """Empty the cart. The cart itself stays ACTIVE and keeps its identity."""
    cart = get_or_create_active_cart(db, user)
    removed = repository.clear_items(db, cart.id)

    if removed:
        audit.record_event(
            db,
            event_type=audit.CART_ITEM_REMOVED,
            actor_user_id=user.id,
            client=client,
            details={"cart_id": str(cart.id), "cleared": True, "items_removed": removed},
        )
    db.commit()


def _remove(db: Session, *, cart: Cart, item: CartItem, user: User, client: ClientInfo) -> Cart:
    variant_id = item.variant_id
    repository.remove_item(db, item)
    audit.record_event(
        db,
        event_type=audit.CART_ITEM_REMOVED,
        actor_user_id=user.id,
        client=client,
        details={"cart_id": str(cart.id), "variant_id": str(variant_id)},
    )
    db.commit()
    return cart


def _require_positive(quantity: int) -> None:
    if quantity < 1:
        raise BusinessRuleError(
            "A cart item quantity must be at least 1.", code="INVALID_CART_ITEM_QUANTITY"
        )


def _require_item_of_cart(db: Session, item_id: uuid.UUID, cart_id: uuid.UUID) -> CartItem:
    item = repository.get_item(db, item_id)
    # An item belonging to somebody else's cart is reported as missing rather
    # than forbidden: from this cart's perspective it does not exist, and
    # saying so confirms nothing about another customer.
    if item is None or item.cart_id != cart_id:
        raise NotFoundError("Cart item not found.", code="CART_ITEM_NOT_FOUND")
    return item


def _require_sellable_variant(db: Session, variant_id: uuid.UUID):
    """A variant a customer may put in a cart for the first time.

    Stricter than the flagging rule below, which governs lines that are
    already there: adding something new that cannot be bought serves nobody.
    """
    variant = catalog_repository.get_variant(db, variant_id)
    if variant is None:
        raise NotFoundError("Variant not found.", code="VARIANT_NOT_FOUND")

    product = catalog_repository.get_product(db, variant.product_id)
    if variant.status != VARIANT_ACTIVE or product is None or product.status != PRODUCT_ACTIVE:
        raise BusinessRuleError(
            "That variant is not available for purchase.", code="VARIANT_NOT_AVAILABLE"
        )
    return variant


# --------------------------------------------------------------------------
# Read model
# --------------------------------------------------------------------------


def build_cart_view(db: Session, cart: Cart) -> dict[str, Any]:
    return {
        "id": cart.id,
        "status": cart.status,
        "items": [_item_view(row) for row in repository.list_item_details(db, cart.id)],
        "created_at": cart.created_at,
        "updated_at": cart.updated_at,
    }


def _item_view(row) -> dict[str, Any]:
    reason = _unavailable_reason(row.variant_status, row.product_status)
    return {
        "id": row.id,
        "variant_id": row.variant_id,
        "product_id": row.product_id,
        "product_name": row.product_name,
        "sku": row.sku,
        "quantity": row.quantity,
        # Read from the catalogue at display time and never stored on the
        # line. The permanent price snapshot belongs to Phase 5 order items.
        "unit_price": row.price,
        "currency": row.currency,
        "available": reason is None,
        "unavailable_reason": reason,
    }


def _unavailable_reason(variant_status: str, product_status: str) -> str | None:
    if variant_status != VARIANT_ACTIVE:
        return UNAVAILABLE_VARIANT_NOT_ACTIVE
    if product_status != PRODUCT_ACTIVE:
        return UNAVAILABLE_PRODUCT_NOT_ACTIVE
    return None
