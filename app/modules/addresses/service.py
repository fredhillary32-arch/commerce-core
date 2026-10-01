"""Address business rules: ownership, the single default, and the snapshot.

Ownership is the authorization. Every function takes the authenticated
``User`` and resolves the address from it, and an address belonging to
somebody else is reported as **missing** rather than forbidden - from this
customer's perspective it does not exist, and saying so confirms nothing
about another customer. That is the same choice the cart makes for items.

The other job of this module is :func:`snapshot`, which is what checkout
copies onto an order. An order's address is a frozen dictionary with no
foreign key back to this table, so everything here stays freely mutable:
editing or deleting a saved address can never rewrite where a past order
was sent.
"""

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, constraint_name
from app.db.models.address import Address
from app.db.models.user import User
from app.modules.addresses import repository
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo

_ONE_DEFAULT_INDEX = "uq_addresses_one_default_per_user"

# Which fields a customer may set, and therefore which ones checkout may
# find on the model. Listed once so the create path, the patch path and the
# snapshot cannot drift apart.
EDITABLE_FIELDS = (
    "label",
    "recipient_name",
    "line1",
    "line2",
    "city",
    "region",
    "postal_code",
    "country_code",
    "phone",
)


def list_addresses(db: Session, user: User) -> list[Address]:
    return repository.list_for_user(db, user.id)


def require_address(db: Session, *, address_id: uuid.UUID, user: User) -> Address:
    address = repository.get_address(db, address_id)
    if address is None or address.user_id != user.id:
        raise NotFoundError("Address not found.", code="ADDRESS_NOT_FOUND")
    return address


def create_address(
    db: Session, *, user: User, client: ClientInfo, is_default: bool = False, **fields
) -> Address:
    fields = _normalise(fields)

    try:
        if is_default:
            repository.clear_default(db, user.id)
        address = repository.add_address(
            db, user_id=user.id, is_default=is_default, **fields
        )
        audit.record_event(
            db,
            event_type=audit.ADDRESS_CREATED,
            actor_user_id=user.id,
            client=client,
            details={"address_id": str(address.id), "is_default": is_default},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(address)
    return address


def update_address(
    db: Session,
    *,
    user: User,
    address_id: uuid.UUID,
    client: ClientInfo,
    is_default: bool | None = None,
    **fields,
) -> Address:
    """Change some of an address. Absent fields are left alone.

    Demoting the current default is *not* offered: ``is_default=False`` on
    the only default would leave a customer with none, which the checkout
    convenience exists to avoid. Promoting a different address is how a
    default moves.
    """
    address = require_address(db, address_id=address_id, user=user)
    fields = _normalise({name: value for name, value in fields.items() if value is not None})

    try:
        for name, value in fields.items():
            setattr(address, name, value)

        if is_default:
            repository.clear_default(db, user.id, except_id=address.id)
            address.is_default = True

        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.refresh(address)
    return address


def delete_address(
    db: Session, *, user: User, address_id: uuid.UUID, client: ClientInfo
) -> None:
    """Remove a saved address.

    Unconditional, and deliberately so: no order points at this row, so
    there is nothing a deletion could orphan. An order keeps its own frozen
    copy of where it was sent.
    """
    address = require_address(db, address_id=address_id, user=user)

    repository.remove_address(db, address)
    audit.record_event(
        db,
        event_type=audit.ADDRESS_DELETED,
        actor_user_id=user.id,
        client=client,
        details={"address_id": str(address.id)},
    )
    db.commit()


def snapshot(address: Address) -> dict[str, Any]:
    """The frozen copy an order keeps of where it was sent.

    Plain JSON-serialisable values only, and no identifier of the row it
    came from: an order must remain readable after the saved address has
    been edited or deleted, and a pointer would invite somebody to follow
    it and read today's version instead of the one that was used.
    """
    return {field: getattr(address, field) for field in EDITABLE_FIELDS}


def _normalise(fields: dict[str, Any]) -> dict[str, Any]:
    """Upper-case the country code, so resolution never misses on case.

    Tax rates and shipping rules are matched on this value, and a rule
    stored as "GB" must match an address a client sent as "gb" - otherwise
    checkout would report a configuration gap that does not exist.
    """
    if fields.get("country_code"):
        fields["country_code"] = fields["country_code"].upper()
    return fields


def _translate(error: IntegrityError) -> Exception:
    if constraint_name(error) == _ONE_DEFAULT_INDEX:
        # Two "make this my default" requests landing together. A 409 says
        # what happened and the retry succeeds; silently picking a winner
        # would leave the customer unsure which one they got.
        return ConflictError(
            "Another request changed your default address; try again.",
            code="DEFAULT_ADDRESS_CONFLICT",
        )
    return error
