"""Tax rates, resolved from the destination address at checkout.

One rate per jurisdiction, applied per order line. There are deliberately no
tax categories, no compound rates and no third-party engine: those are real
requirements for some businesses and none of them were asked for, so adding
them now would be inventing a domain model nobody has specified.

**A missing rate is a rejection, not a zero.** "No tax applies in this
country" is expressed by configuring a deliberate ``0.0000`` rate row. That
makes zero-tax a decision an operator made rather than a country nobody got
round to configuring, and it is why the absence of a row stops the sale.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TaxRate(Base):
    """The rate charged on goods shipped to one jurisdiction.

    ``rate`` is a fraction, not a percentage: ``0.2000`` is 20%.
    ``NUMERIC(6,4)`` therefore caps a rate just under 100% with four decimal
    places - ample for VAT and sales tax, and a deliberate limit rather than
    an oversight.

    A null ``region`` means the rate covers the whole country. A row naming
    a region is preferred over the country-wide row when both match, so a
    country-wide default and a handful of regional exceptions can coexist.
    """

    __tablename__ = "tax_rates"
    __table_args__ = (
        CheckConstraint("rate >= 0 AND rate <= 1", name="rate_range"),
        # Partial on is_active, so superseded rates stay as history while at
        # most one live rate exists per jurisdiction. COALESCE is what makes
        # the country-wide row participate: two NULL regions are distinct to
        # a plain unique index, which would allow two live country-wide
        # rates for the same country and make resolution non-deterministic.
        Index(
            "uq_tax_rates_active_jurisdiction",
            "country_code",
            text("coalesce(region, '')"),
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
