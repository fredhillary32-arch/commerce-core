"""Shipping rules: what delivery costs, decided from destination and value.

A rule is a fixed cost for a destination country and a band of order
subtotals. There is no carrier integration, no weight, no dimensions and no
live rating call - none of that was specified, and a guessed weight model
would be worse than an explicit table an operator controls.

Free shipping above a threshold is not a separate mechanism: it is a rule
with ``cost = 0`` and a ``min_order_subtotal``. **A missing rule stops the
sale** rather than shipping free, because accidental free shipping costs
real money on every order while a configuration gap is something an
operator should be shown.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ShippingRule(Base):
    """One delivery price for one country and one band of order values.

    Bands are inclusive at both ends and may legitimately overlap - that is
    what ``priority`` is for. When several rules match, the highest priority
    wins; ties go to the narrowest band, so a specific promotional band
    beats a catch-all without needing its priority raised.

    ``max_order_subtotal`` is null for an open-ended top band.
    """

    __tablename__ = "shipping_rules"
    __table_args__ = (
        CheckConstraint("cost >= 0", name="cost_non_negative"),
        CheckConstraint("min_order_subtotal >= 0", name="min_order_subtotal_non_negative"),
        # An inverted band would match nothing while looking like valid
        # configuration - the kind of mistake that is only noticed when a
        # customer cannot check out.
        CheckConstraint(
            "max_order_subtotal IS NULL OR max_order_subtotal > min_order_subtotal",
            name="band_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False, index=True)
    min_order_subtotal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    max_order_subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # A rule prices an order, so it must be quoted in the order's currency.
    # Checkout refuses a mismatch rather than converting: this system has no
    # exchange rates and inventing one silently would be worse than failing.
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
