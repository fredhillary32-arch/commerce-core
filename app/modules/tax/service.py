"""Tax business rules: one rate per jurisdiction, and what a missing one means.

The conviction here is short: **a missing rate stops the sale.** Charging
0% because nobody configured a country is indistinguishable, at the moment
it happens, from charging 0% because no tax applies - and the two have very
different consequences when the tax authority asks. So "no tax applies
here" has to be written down, as a deliberate ``0.0000`` rate row, and the
absence of any row is treated as the configuration gap it is.

Resolution is by destination: the shipping address decides the rate, and
the rate is applied per order line and snapshotted onto it.
"""

import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    constraint_name,
)
from app.db.models.tax import TaxRate
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.tax import repository

_ONE_ACTIVE_RATE_INDEX = "uq_tax_rates_active_jurisdiction"


def list_rates(db: Session, *, limit: int, offset: int) -> list[TaxRate]:
    return repository.list_rates(db, limit=limit, offset=offset)


def require_rate(db: Session, rate_id: uuid.UUID) -> TaxRate:
    rate = repository.get_rate(db, rate_id)
    if rate is None:
        raise NotFoundError("Tax rate not found.", code="TAX_RATE_NOT_FOUND")
    return rate


def create_rate(db: Session, *, actor: User, client: ClientInfo, **fields) -> TaxRate:
    fields["country_code"] = fields["country_code"].upper()

    try:
        rate = repository.add_rate(db, **fields)
        audit.record_event(
            db,
            event_type=audit.TAX_RATE_CHANGED,
            actor_user_id=actor.id,
            client=client,
            details={
                "tax_rate_id": str(rate.id),
                "action": "created",
                "country_code": rate.country_code,
                "region": rate.region,
                "rate": str(rate.rate),
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(rate)
    return rate


def update_rate(
    db: Session, *, rate_id: uuid.UUID, actor: User, client: ClientInfo, **fields
) -> TaxRate:
    """Amend a rate, or retire it.

    Rates are not deleted. Retiring one by setting ``is_active`` false keeps
    it readable by the orders that were taxed under it, and frees the
    jurisdiction for its replacement.
    """
    rate = require_rate(db, rate_id)
    if fields.get("country_code"):
        fields["country_code"] = fields["country_code"].upper()

    try:
        for name, value in fields.items():
            setattr(rate, name, value)
        audit.record_event(
            db,
            event_type=audit.TAX_RATE_CHANGED,
            actor_user_id=actor.id,
            client=client,
            details={"tax_rate_id": str(rate.id), "action": "updated", **_stringify(fields)},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(rate)
    return rate


def resolve_rate(db: Session, *, country_code: str, region: str | None) -> TaxRate:
    """The rate that applies to a delivery, or a refusal to guess.

    Called by checkout with the **shipping** address, which is where the
    goods are consumed and therefore what decides the rate here.
    """
    rate = repository.resolve(db, country_code=country_code.upper(), region=region)
    if rate is None:
        raise BusinessRuleError(
            f"No tax rate is configured for {country_code.upper()}; "
            "orders cannot be placed to that destination.",
            code="NO_TAX_RATE",
        )
    return rate


def snapshot(rate: TaxRate) -> dict[str, object]:
    """What the order keeps, so its tax can be explained years later."""
    return {
        "tax_rate_id": str(rate.id),
        "name": rate.name,
        "rate": str(rate.rate),
        "country_code": rate.country_code,
        "region": rate.region,
    }


def _stringify(fields: dict) -> dict:
    return {
        name: str(value) if isinstance(value, Decimal) else value
        for name, value in fields.items()
    }


def _translate(error: IntegrityError) -> Exception:
    if constraint_name(error) == _ONE_ACTIVE_RATE_INDEX:
        return ConflictError(
            "An active tax rate already exists for that country and region; "
            "retire it before adding another.",
            code="TAX_RATE_ALREADY_EXISTS",
        )
    # Unmapped constraints surface as a 500 by design, as everywhere else:
    # a plausible but wrong 409 would be worse than an obvious failure.
    return error
