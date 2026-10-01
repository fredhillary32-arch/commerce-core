"""Every shipping-rule database query. No business rules live here."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.shipping import ShippingRule

# Stands in for "no upper bound" when ranking bands by width. Larger than
# any order NUMERIC(12,2) can hold, so an open-ended band always sorts as
# the widest and therefore the least specific.
_UNBOUNDED = Decimal("9999999999.99")


def get_rule(db: Session, rule_id: uuid.UUID) -> ShippingRule | None:
    return db.get(ShippingRule, rule_id)


def list_rules(db: Session, *, limit: int, offset: int) -> list[ShippingRule]:
    return list(
        db.execute(
            select(ShippingRule)
            .order_by(
                ShippingRule.country_code,
                ShippingRule.priority.desc(),
                ShippingRule.min_order_subtotal,
                ShippingRule.id,
            )
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def add_rule(db: Session, **fields) -> ShippingRule:
    rule = ShippingRule(**fields)
    db.add(rule)
    db.flush()
    return rule


def resolve(db: Session, *, country_code: str, subtotal: Decimal) -> ShippingRule | None:
    """The rule that prices a delivery, by the documented precedence.

    Bands may legitimately overlap - a permanent rate and a promotional one
    covering part of it - so the tie-break is stated in the ORDER BY rather
    than left to insertion order:

    1. highest ``priority`` wins;
    2. then the narrowest band, so a specific promotional range beats a
       catch-all without anyone having to remember to raise its priority;
    3. then ``id``, so the result is deterministic even when two rules are
       genuinely indistinguishable.
    """
    width = func.coalesce(ShippingRule.max_order_subtotal, _UNBOUNDED) - (
        ShippingRule.min_order_subtotal
    )
    return db.execute(
        select(ShippingRule)
        .where(
            ShippingRule.country_code == country_code,
            ShippingRule.is_active.is_(True),
            ShippingRule.min_order_subtotal <= subtotal,
            (ShippingRule.max_order_subtotal.is_(None))
            | (ShippingRule.max_order_subtotal >= subtotal),
        )
        .order_by(ShippingRule.priority.desc(), width.asc(), ShippingRule.id)
        .limit(1)
    ).scalar_one_or_none()
