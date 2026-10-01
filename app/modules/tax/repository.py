"""Every tax-rate database query. No business rules live here."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.tax import TaxRate


def get_rate(db: Session, rate_id: uuid.UUID) -> TaxRate | None:
    return db.get(TaxRate, rate_id)


def list_rates(db: Session, *, limit: int, offset: int) -> list[TaxRate]:
    return list(
        db.execute(
            select(TaxRate)
            .order_by(TaxRate.country_code, TaxRate.region.nulls_first(), TaxRate.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def add_rate(db: Session, **fields) -> TaxRate:
    rate = TaxRate(**fields)
    db.add(rate)
    db.flush()
    return rate


def resolve(db: Session, *, country_code: str, region: str | None) -> TaxRate | None:
    """The active rate for one destination, most specific first.

    A row naming the destination's region wins over the country-wide row;
    an address whose region matches no row falls back to the country-wide
    one. Expressed as a single ordered query rather than two round trips so
    that the precedence is stated once, in SQL, and cannot drift between a
    "try the region" call and a "try the country" call.
    """
    return db.execute(
        select(TaxRate)
        .where(
            TaxRate.country_code == country_code,
            TaxRate.is_active.is_(True),
            # Either the country-wide row, or the row for exactly this region.
            (TaxRate.region.is_(None)) | (TaxRate.region == region),
        )
        # NULLS LAST: a region-specific row sorts before the country-wide one.
        .order_by(TaxRate.region.desc().nulls_last())
        .limit(1)
    ).scalar_one_or_none()


def count_active_for_jurisdiction(db: Session, *, country_code: str, region: str | None) -> int:
    return db.execute(
        select(func.count())
        .select_from(TaxRate)
        .where(
            TaxRate.country_code == country_code,
            TaxRate.is_active.is_(True),
            TaxRate.region.is_(None) if region is None else TaxRate.region == region,
        )
    ).scalar_one()
