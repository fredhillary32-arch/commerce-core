"""Shipping business rules: what delivery costs, and what a missing rule means.

The same conviction as tax, for a different reason: **a missing rule stops
the sale.** Shipping free by default costs the merchant real money on every
order to an unconfigured destination, and does it silently. A rule that is
genuinely free is a rule with ``cost = 0``, which somebody chose.

Phase 7 will add shipments and shipment events to this module. Phase 5 does
nothing but price the delivery and freeze that price onto the order.
"""

import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessRuleError, NotFoundError, constraint_name
from app.db.models.shipping import ShippingRule
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.shipping import repository

_BAND_CONSTRAINT = "ck_shipping_rules_band_valid"


def list_rules(db: Session, *, limit: int, offset: int) -> list[ShippingRule]:
    return repository.list_rules(db, limit=limit, offset=offset)


def require_rule(db: Session, rule_id: uuid.UUID) -> ShippingRule:
    rule = repository.get_rule(db, rule_id)
    if rule is None:
        raise NotFoundError("Shipping rule not found.", code="SHIPPING_RULE_NOT_FOUND")
    return rule


def create_rule(db: Session, *, actor: User, client: ClientInfo, **fields) -> ShippingRule:
    fields["country_code"] = fields["country_code"].upper()
    fields["currency"] = fields["currency"].upper()

    try:
        rule = repository.add_rule(db, **fields)
        audit.record_event(
            db,
            event_type=audit.SHIPPING_RULE_CHANGED,
            actor_user_id=actor.id,
            client=client,
            details={
                "shipping_rule_id": str(rule.id),
                "action": "created",
                "country_code": rule.country_code,
                "cost": str(rule.cost),
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(rule)
    return rule


def update_rule(
    db: Session, *, rule_id: uuid.UUID, actor: User, client: ClientInfo, **fields
) -> ShippingRule:
    """Amend a rule, or retire it by clearing ``is_active``.

    Rules are not deleted, for the same reason rates are not: an order's
    snapshot explains what it was charged, and the row it came from is
    worth keeping alongside it.
    """
    rule = require_rule(db, rule_id)
    for key in ("country_code", "currency"):
        if fields.get(key):
            fields[key] = fields[key].upper()

    try:
        for name, value in fields.items():
            setattr(rule, name, value)
        audit.record_event(
            db,
            event_type=audit.SHIPPING_RULE_CHANGED,
            actor_user_id=actor.id,
            client=client,
            details={
                "shipping_rule_id": str(rule.id),
                "action": "updated",
                **{
                    name: str(value) if isinstance(value, Decimal) else value
                    for name, value in fields.items()
                },
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(rule)
    return rule


def resolve_rule(
    db: Session, *, country_code: str, subtotal: Decimal, currency: str
) -> ShippingRule:
    """The rule that prices this delivery, or a refusal to guess.

    The currency check is deliberately a rejection rather than a
    conversion. This system holds no exchange rates, and inventing one at
    checkout would quietly charge the wrong amount; a mismatch is a
    configuration error an operator can fix in seconds.
    """
    rule = repository.resolve(db, country_code=country_code.upper(), subtotal=subtotal)
    if rule is None:
        raise BusinessRuleError(
            f"No shipping rule covers an order of {subtotal} to "
            f"{country_code.upper()}; orders cannot be placed to that destination.",
            code="NO_SHIPPING_RULE",
        )

    if rule.currency != currency:
        raise BusinessRuleError(
            f"The shipping rule for {rule.country_code} is priced in "
            f"{rule.currency}, but this order is in {currency}.",
            code="SHIPPING_RULE_CURRENCY_MISMATCH",
        )
    return rule


def snapshot(rule: ShippingRule) -> dict[str, object]:
    """What the order keeps, so its delivery charge stays explicable."""
    return {
        "shipping_rule_id": str(rule.id),
        "name": rule.name,
        "country_code": rule.country_code,
        "cost": str(rule.cost),
        "currency": rule.currency,
        "min_order_subtotal": str(rule.min_order_subtotal),
        "max_order_subtotal": (
            None if rule.max_order_subtotal is None else str(rule.max_order_subtotal)
        ),
    }


def _translate(error: IntegrityError) -> Exception:
    if constraint_name(error) == _BAND_CONSTRAINT:
        # An inverted band matches nothing while looking like valid
        # configuration - the kind of mistake only noticed when a customer
        # cannot check out. PostgreSQL refuses it; this says why.
        return BusinessRuleError(
            "A shipping rule's maximum order subtotal must be above its minimum.",
            code="INVALID_SHIPPING_RULE",
        )
    return error
