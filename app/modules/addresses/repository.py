"""Every address database query. No business rules live here."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models.address import Address


def get_address(db: Session, address_id: uuid.UUID) -> Address | None:
    return db.get(Address, address_id)


def list_for_user(db: Session, user_id: uuid.UUID) -> list[Address]:
    return list(
        db.execute(
            select(Address)
            .where(Address.user_id == user_id)
            # Default first, then oldest first: the address a customer is
            # most likely to want is the one at the top of the list.
            .order_by(Address.is_default.desc(), Address.created_at, Address.id)
        ).scalars()
    )


def add_address(db: Session, *, user_id: uuid.UUID, **fields) -> Address:
    address = Address(user_id=user_id, **fields)
    db.add(address)
    db.flush()
    return address


def clear_default(db: Session, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None) -> None:
    """Demote whichever address is currently this user's default.

    Issued as one UPDATE rather than a read-then-write so there is no gap
    between finding the old default and clearing it, and flushed
    immediately so the partial unique index sees the demotion before the
    promotion that follows it.
    """
    statement = update(Address).where(Address.user_id == user_id, Address.is_default.is_(True))
    if except_id is not None:
        statement = statement.where(Address.id != except_id)
    db.execute(statement.values(is_default=False))
    db.flush()


def remove_address(db: Session, address: Address) -> None:
    db.delete(address)
