"""Addresses a customer has saved for reuse at checkout.

A saved address is a **convenience, never a record**. What an order was
shipped to is frozen onto the order itself as a JSONB snapshot with no
foreign key back here, so editing or deleting a row in this table can never
change where a past order says it went. That separation is the whole reason
this table is allowed to be freely mutable.
"""

import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Address(Base):
    """One address belonging to one customer.

    Ownership is the authorization, exactly as for carts: every route
    resolves the owner from the access token and no request body carries a
    ``user_id``, so there is no identifier a caller could change to reach
    somebody else's address.
    """

    __tablename__ = "addresses"
    __table_args__ = (
        # Partial, the same shape as the catalogue's one-primary-image index
        # and the cart's one-ACTIVE-cart index: a customer may keep any
        # number of addresses but at most one marked default. Making it a
        # database rule rather than a service rule is what stops a
        # concurrent "set default" pair from leaving two.
        Index(
            "uq_addresses_one_default_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # What the customer calls it ("Home", "Office"). Free text, never used
    # for resolution - two addresses may share a label.
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    line1: Mapped[str] = mapped_column(String(255), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    # Nullable because plenty of countries have no meaningful subdivision in
    # a postal address. Tax resolution treats a null region as "the whole
    # country", which is exactly what it means here.
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # ISO 3166-1 alpha-2, and the join key for both tax and shipping rules.
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
