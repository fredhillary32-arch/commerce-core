"""Checkout and order business rules.

Three convictions run through this module.

First, **checkout is one transaction.** Every reservation, the order, its
lines and the cart's transition to CONVERTED commit together or not at all.
This is why Phase 4's reservation functions were split into a committing
wrapper and a composable ``*_locked`` half: a partially successful checkout
is not something to detect and compensate for, it is something that cannot
happen. There is no cleanup code here because there is nothing to clean up.

Second, **an order is a snapshot.** Prices, names, SKUs, tax rates and both
addresses are copied onto it. Nothing about a placed order is read back
from the catalogue, the tax table or the address book, so none of them can
rewrite history by changing.

Third, **checkout performs no availability arithmetic of its own.** It asks
``inventory.reserve_locked`` for stock and believes the answer. The single
definition of what is available lives on the ``Inventory`` model, and a
second copy here would be a second thing to get wrong.
"""

import secrets
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessRuleError, NotFoundError, constraint_name
from app.db.models.cart import CART_CONVERTED
from app.db.models.catalog import PRODUCT_ACTIVE, VARIANT_ACTIVE
from app.db.models.order import (
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_PENDING_PAYMENT,
    Order,
)
from app.db.models.user import User
from app.modules.addresses import service as addresses
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.cart import repository as cart_repository
from app.modules.catalog import repository as catalog_repository
from app.modules.inventory import service as inventory
from app.modules.orders import repository
from app.modules.shipping import service as shipping
from app.modules.tax import service as tax

# Which moves are legal. Same explicit-table shape as the product, variant
# and reservation state machines, so there is one place to read the rules.
#
# CONFIRMED is terminal **in this phase**: cancelling a paid order implies a
# refund (Phase 6) and a restock (Phase 7), and a cancel path that could do
# neither would be worse than not having one.
ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    ORDER_PENDING_PAYMENT: frozenset({ORDER_CONFIRMED, ORDER_CANCELLED}),
    ORDER_CONFIRMED: frozenset(),
    ORDER_CANCELLED: frozenset(),
}

_CENTS = Decimal("0.01")

# No 0, 1, I, L, O or U. A customer may read this reference aloud to
# somebody in support, and those six are the characters people mishear.
_REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_REFERENCE_GROUPS = 3
_REFERENCE_GROUP_SIZE = 4
_REFERENCE_ATTEMPTS = 5

_ONE_ORDER_PER_CART = "uq_orders_cart_id"


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------


def checkout(
    db: Session,
    *,
    user: User,
    shipping_address_id: uuid.UUID,
    billing_address_id: uuid.UUID | None,
    client: ClientInfo,
) -> tuple[Order, bool]:
    """Turn the caller's cart into an order. Returns ``(order, created)``.

    The steps, in the order they must happen:

    1. lock the caller's ACTIVE cart;
    2. read its lines with the catalogue facts they need;
    3. validate every line before anything is written;
    4. resolve the addresses, which must belong to the caller;
    5. resolve the tax rate and shipping rule for the destination;
    6. compute the money;
    7. reserve every line, **sorted by variant id**;
    8. insert the order and its lines;
    9. move the cart to CONVERTED;
    10. commit - the single boundary.

    Steps 7 to 9 hold one inventory row lock per line until step 10.
    Sorting by variant id is what makes that safe: every checkout acquires
    its locks in the same global order, so two carts holding the same two
    variants in opposite order cannot form a cycle.

    ``created`` is false when this call found an order that already existed
    for this cart - the replay of a submit whose response was lost.
    """
    cart = cart_repository.lock_active_cart(db, user.id)
    if cart is None:
        # No live basket. Either this is the retry of a checkout that
        # already succeeded - the only way to have no ACTIVE cart - or the
        # customer genuinely has nothing.
        replayed = repository.latest_order_for_user(db, user.id)
        if replayed is not None:
            return replayed, False
        raise NotFoundError("You have no active cart.", code="CART_NOT_FOUND")

    lines = cart_repository.list_item_details(db, cart.id)
    if not lines:
        raise BusinessRuleError("Your cart is empty.", code="CART_EMPTY")

    _require_purchasable(lines)
    currency = _single_currency(lines)

    shipping_address = addresses.require_address(
        db, address_id=shipping_address_id, user=user
    )
    billing_address = (
        shipping_address
        if billing_address_id is None
        else addresses.require_address(db, address_id=billing_address_id, user=user)
    )

    tax_rate = tax.resolve_rate(
        db, country_code=shipping_address.country_code, region=shipping_address.region
    )
    priced = [_price_line(line, tax_rate.rate) for line in lines]
    subtotal = sum((line["line_subtotal"] for line in priced), Decimal("0.00"))
    tax_total = sum((line["tax_amount"] for line in priced), Decimal("0.00"))

    shipping_rule = shipping.resolve_rule(
        db,
        country_code=shipping_address.country_code,
        subtotal=subtotal,
        currency=currency,
    )

    # Captured as a plain value: the rollback below expires every object
    # this session holds, and the recovery path still needs the id.
    cart_id = cart.id

    try:
        # Step 7. Sorted, and every lock held until the commit below.
        reservations = [
            _reserve_line(db, line=line, cart_id=cart_id, user=user, client=client)
            for line in sorted(priced, key=_acquisition_order)
        ]

        order = _write_order(
            db,
            user=user,
            cart=cart,
            currency=currency,
            priced=priced,
            subtotal=subtotal,
            tax_total=tax_total,
            shipping_rule=shipping_rule,
            tax_rate=tax_rate,
            shipping_address=shipping_address,
            billing_address=billing_address,
            expires_at=min(reservation.expires_at for reservation in reservations),
        )

        # Step 9. The cart keeps its identity and its history; it simply
        # stops being the live basket, which is also what stops the same
        # cart being checked out again.
        cart.status = CART_CONVERTED

        audit.record_event(
            db,
            event_type=audit.ORDER_PLACED,
            actor_user_id=user.id,
            client=client,
            details={
                "order_id": str(order.id),
                "reference": order.reference,
                "cart_id": str(cart.id),
                "lines": len(priced),
                "total_amount": str(order.total_amount),
                "currency": currency,
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if constraint_name(error) != _ONE_ORDER_PER_CART:
            raise
        # Two submits of the same cart landed together and this one lost.
        # The winner's order is the right answer to both, so return it
        # rather than reporting a conflict the caller cannot act on.
        existing = repository.get_order_by_cart(db, cart_id)
        if existing is None:
            raise
        return existing, False
    except Exception:
        # Every rejection, every failure: one rollback, nothing half-written.
        db.rollback()
        raise

    db.refresh(order)
    return order, True


def _acquisition_order(line) -> str:
    """The order in which checkout takes its inventory row locks.

    Checkout holds one lock per line until it commits, so two carts holding
    the same two variants in opposite order would otherwise be able to form
    a cycle - each holding what the other wants next - and PostgreSQL would
    kill one with a deadlock error.

    Sorting by variant id gives every checkout in the system the same global
    acquisition sequence, which makes a cycle impossible rather than
    unlikely. The same key orders confirmation and cancellation, so those
    cannot deadlock against a checkout either.

    A named function rather than an inline lambda so that the property can
    be broken deliberately in a test and the resulting deadlock observed.
    """
    return str(line["variant_id"])


def _reserve_line(db: Session, *, line, cart_id, user, client):
    """Hold one line's stock, and say which line it was if the hold fails.

    ``reserve_locked`` knows about variants and quantities, not about SKUs,
    so its message cannot name the line. A customer told only "there is not
    enough stock" has no way to tell which of five items to reduce.
    """
    try:
        return inventory.reserve_locked(
            db,
            variant_id=line["variant_id"],
            cart_id=cart_id,
            quantity=line["quantity"],
            actor=user,
            client=client,
        )
    except BusinessRuleError as error:
        if error.code != "INSUFFICIENT_INVENTORY":
            raise
        raise BusinessRuleError(
            f"There is not enough stock of {line['sku']} to order "
            f"{line['quantity']}.",
            code="INSUFFICIENT_INVENTORY",
        ) from None


def _write_order(db: Session, *, user, cart, currency, priced, subtotal, tax_total,
                 shipping_rule, tax_rate, shipping_address, billing_address,
                 expires_at) -> Order:
    """Step 8: the order header and one line per cart item."""
    total = subtotal + tax_total + shipping_rule.cost

    order = repository.add_order(
        db,
        reference=_new_reference(db),
        user_id=user.id,
        cart_id=cart.id,
        status=ORDER_PENDING_PAYMENT,
        currency=currency,
        subtotal_amount=subtotal,
        discount_total=Decimal("0.00"),
        tax_total=tax_total,
        shipping_total=shipping_rule.cost,
        total_amount=total,
        shipping_address=addresses.snapshot(shipping_address),
        billing_address=addresses.snapshot(billing_address),
        tax_rate_snapshot=tax.snapshot(tax_rate),
        shipping_rule_snapshot=shipping.snapshot(shipping_rule),
        reservation_expires_at=expires_at,
    )

    summaries = _option_summaries(db, [line["variant_id"] for line in priced])
    for line in priced:
        repository.add_item(
            db,
            order_id=order.id,
            variant_id=line["variant_id"],
            product_id=line["product_id"],
            product_name=line["product_name"],
            sku=line["sku"],
            variant_option_summary=summaries.get(line["variant_id"]),
            quantity=line["quantity"],
            unit_price=line["unit_price"],
            currency=currency,
            discount_amount=Decimal("0.00"),
            tax_rate=tax_rate.rate,
            tax_amount=line["tax_amount"],
            line_subtotal=line["line_subtotal"],
            line_total=line["line_total"],
        )

    # The one arithmetic rule a CHECK constraint cannot express, because it
    # spans rows. Asserted here and tested, the same compromise the
    # catalogue makes for category cycles.
    if order.total_amount != subtotal - order.discount_total + tax_total + shipping_rule.cost:
        raise BusinessRuleError(
            "The order total does not reconcile against its lines.",
            code="ORDER_TOTAL_MISMATCH",
        )
    return order


# --------------------------------------------------------------------------
# Line validation and money
# --------------------------------------------------------------------------


def _require_purchasable(lines) -> None:
    """Every line must still be buyable, or none of the order is placed.

    The cart deliberately keeps a line whose variant stopped being sellable
    and flags it, rather than deleting something the customer chose.
    Checkout is where that becomes a refusal.
    """
    for line in lines:
        if line.variant_status != VARIANT_ACTIVE or line.product_status != PRODUCT_ACTIVE:
            raise BusinessRuleError(
                f"{line.sku} is no longer available for purchase.",
                code="VARIANT_NOT_AVAILABLE",
            )
        if line.price is None or line.currency is None:
            # An ACTIVE variant always has a price, but a line may have been
            # sitting in the cart since before that was true. Unpriced is
            # unsellable whatever the status says.
            raise BusinessRuleError(
                f"{line.sku} has no price and cannot be bought.",
                code="VARIANT_NOT_AVAILABLE",
            )


def _single_currency(lines) -> str:
    currencies = {line.currency for line in lines}
    if len(currencies) > 1:
        raise BusinessRuleError(
            "Your cart contains items priced in more than one currency; "
            f"found {', '.join(sorted(currencies))}.",
            code="MIXED_CURRENCY_CART",
        )
    return currencies.pop()


def _price_line(line, rate: Decimal) -> dict[str, Any]:
    """One line's money, rounded once, here.

    Rounded **per line and then summed**, rather than summed and rounded,
    so that a customer adding up the amounts printed against each line gets
    the printed total exactly.
    """
    line_subtotal = (line.price * line.quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
    tax_amount = (line_subtotal * rate).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return {
        "variant_id": line.variant_id,
        "product_id": line.product_id,
        "product_name": line.product_name,
        "sku": line.sku,
        "quantity": line.quantity,
        "unit_price": line.price,
        "line_subtotal": line_subtotal,
        "tax_amount": tax_amount,
        "line_total": line_subtotal + tax_amount,
    }


def _option_summaries(db: Session, variant_ids) -> dict[uuid.UUID, str]:
    """"Size: Large, Colour: Blue" per variant, frozen onto the line.

    Read once for the whole order. The options themselves may be renamed or
    deleted later; the order keeps what they said at the time.
    """
    summaries: dict[uuid.UUID, list[str]] = {}
    for row in catalog_repository.list_assignment_details(db, list(variant_ids)):
        summaries.setdefault(row.variant_id, []).append(f"{row.option_name}: {row.value}")
    return {variant_id: ", ".join(parts) for variant_id, parts in summaries.items()}


def _new_reference(db: Session) -> str:
    """A readable, unguessable order reference.

    Deliberately **not** a sequence. Sequential references let anyone place
    two orders and read the merchant's volume from the gap between them,
    and that costs nothing to avoid.

    Twelve characters from a thirty-character alphabet is about 5.9e17
    possibilities, so a collision is vanishingly unlikely; the pre-check
    below removes the practical case and ``uq_orders_reference`` remains the
    actual guarantee.
    """
    for _ in range(_REFERENCE_ATTEMPTS):
        groups = [
            "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(_REFERENCE_GROUP_SIZE))
            for _ in range(_REFERENCE_GROUPS)
        ]
        candidate = "ORD-" + "-".join(groups)
        if not repository.reference_exists(db, candidate):
            return candidate

    raise BusinessRuleError(
        "Could not allocate an order reference; please try again.",
        code="ORDER_REFERENCE_UNAVAILABLE",
    )


# --------------------------------------------------------------------------
# Reading orders
# --------------------------------------------------------------------------


def require_order_for_user(db: Session, *, order_id: uuid.UUID, user: User) -> Order:
    """One of the caller's own orders.

    Somebody else's order is reported as **missing** rather than forbidden,
    matching the cart and address precedent: from this customer's
    perspective it does not exist, and saying so confirms nothing.
    """
    order = repository.get_order(db, order_id)
    if order is None or order.user_id != user.id:
        raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")
    return order


def require_order(db: Session, order_id: uuid.UUID) -> Order:
    """One order, for an administrator who may see any of them."""
    order = repository.get_order(db, order_id)
    if order is None:
        raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")
    return order


def list_orders_for_user(db: Session, user: User, *, limit: int, offset: int) -> list[Order]:
    return repository.list_for_user(db, user.id, limit=limit, offset=offset)


def list_orders(db: Session, *, limit: int, offset: int, status: str | None = None):
    return repository.list_all(db, limit=limit, offset=offset, status=status)


def build_order_view(db: Session, order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "reference": order.reference,
        "status": order.status,
        "currency": order.currency,
        "subtotal_amount": order.subtotal_amount,
        "discount_total": order.discount_total,
        "tax_total": order.tax_total,
        "shipping_total": order.shipping_total,
        "total_amount": order.total_amount,
        "shipping_address": order.shipping_address,
        "billing_address": order.billing_address,
        "tax_rate_snapshot": order.tax_rate_snapshot,
        "shipping_rule_snapshot": order.shipping_rule_snapshot,
        "reservation_expires_at": order.reservation_expires_at,
        "placed_at": order.placed_at,
        "confirmed_at": order.confirmed_at,
        "cancelled_at": order.cancelled_at,
        "items": repository.list_items(db, order.id),
    }


# --------------------------------------------------------------------------
# Confirmation and cancellation
# --------------------------------------------------------------------------


def confirm_order(
    db: Session,
    *,
    order_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> Order:
    """Turn a paid order into a sale. **Service layer only - no HTTP route.**

    Phase 6 is its only caller, following the precedent Phase 4 set for
    ``reserve``: an endpoint invented now would be replaced then.

    Every reservation converts, or none does, in one transaction - the same
    single-boundary rule checkout follows. The reservations are visited in
    variant-id order, so this can never deadlock against a concurrent
    checkout or cancellation.
    """
    try:
        order = _lock_for_transition(db, order_id, ORDER_CONFIRMED)
        holds = repository.active_reservations_for_cart(db, order.cart_id)

        if len(holds) != len(repository.list_items(db, order.id)):
            # A hold lapsed while the customer was paying. Phase 5 has no
            # worker to have warned them, and re-reserving here would be
            # Phase 6's job - so this refuses rather than confirming an
            # order whose stock is no longer held.
            raise BusinessRuleError(
                "This order's stock reservations are no longer held; "
                "the payment window has lapsed.",
                code="INSUFFICIENT_INVENTORY",
            )

        for hold in holds:
            inventory.convert_reservation_locked(
                db, reservation_id=hold.id, actor=actor, client=client
            )

        order.status = ORDER_CONFIRMED
        order.confirmed_at = _now(db)
        audit.record_event(
            db,
            event_type=audit.ORDER_CONFIRMED,
            actor_user_id=actor.id if actor else None,
            client=client,
            details={"order_id": str(order.id), "reference": order.reference},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(order)
    return order


def cancel_order(
    db: Session,
    *,
    order_id: uuid.UUID,
    actor: User | None = None,
    client: ClientInfo | None = None,
) -> Order:
    """Give up an unpaid order: release its stock and hand the basket back.

    The cart that became this order stays CONVERTED, which keeps both the
    order's route to its reservations and the one-order-per-cart guarantee
    intact. The lines are copied into a fresh ACTIVE cart instead, because
    telling a customer to rebuild their basket from scratch after a failed
    payment is poor enough treatment to be worth the small amount of code.

    Lines whose variants have since stopped being sellable come across
    anyway and are flagged unavailable by the cart's existing read logic -
    behaviour that already exists and is reused rather than special-cased.
    """
    try:
        order = _lock_for_transition(db, order_id, ORDER_CANCELLED)

        for hold in repository.active_reservations_for_cart(db, order.cart_id):
            inventory.release_reservation_locked(
                db, reservation_id=hold.id, actor=actor, client=client
            )

        _restore_basket(db, order)

        order.status = ORDER_CANCELLED
        order.cancelled_at = _now(db)
        audit.record_event(
            db,
            event_type=audit.ORDER_CANCELLED,
            actor_user_id=actor.id if actor else None,
            client=client,
            details={
                "order_id": str(order.id),
                "reference": order.reference,
                "cancelled_by_owner": bool(actor and actor.id == order.user_id),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(order)
    return order


def _restore_basket(db: Session, order: Order) -> None:
    """Copy a cancelled order's lines back into the customer's live cart.

    Into the existing ACTIVE cart if the customer has already started
    shopping again - incrementing rather than duplicating, exactly as
    adding an item twice does - and into a new one otherwise.
    """
    cart = cart_repository.get_active_cart(db, order.user_id)
    if cart is None:
        cart = cart_repository.add_cart(db, user_id=order.user_id)

    for item in repository.list_items(db, order.id):
        existing = cart_repository.get_item_by_variant(
            db, cart_id=cart.id, variant_id=item.variant_id
        )
        if existing is not None:
            existing.quantity += item.quantity
        else:
            cart_repository.add_item(
                db, cart_id=cart.id, variant_id=item.variant_id, quantity=item.quantity
            )


def _lock_for_transition(db: Session, order_id: uuid.UUID, target: str) -> Order:
    """Lock the order row, then decide whether the move is legal.

    The order matters, for the same reason it does for reservations: two
    concurrent cancellations must not both read PENDING_PAYMENT and both
    release the stock.
    """
    order = repository.lock_order(db, order_id)
    if order is None:
        raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")

    if target not in ORDER_TRANSITIONS.get(order.status, frozenset()):
        raise BusinessRuleError(
            f"An order cannot move from {order.status} to {target}.",
            code="INVALID_ORDER_STATE",
        )
    return order


def _now(db: Session):
    """The database's clock, so timestamps agree with the server defaults.

    Taken from PostgreSQL rather than Python: ``placed_at`` and the other
    timestamps default to ``now()`` server-side, and mixing two clocks
    would make an order's own timestamps disagree by the network round trip.
    """
    return db.execute(select(func.now())).scalar_one()


__all__ = [
    "ORDER_TRANSITIONS",
    "build_order_view",
    "cancel_order",
    "checkout",
    "confirm_order",
    "list_orders",
    "list_orders_for_user",
    "require_order",
    "require_order_for_user",
]
