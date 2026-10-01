"""Address HTTP endpoints.

Authentication only, no permission code, and no path or body field naming a
user. Ownership *is* the authorization - see
``app.modules.addresses.service`` for why an ``addresses.*`` permission
would be actively wrong under the Phase 2 zero-roles rule.
"""

import uuid

from fastapi import APIRouter, Request, status

from app.modules.addresses import service
from app.modules.addresses.dependencies import CurrentUser, DbSession
from app.modules.addresses.schemas import AddressCreate, AddressRead, AddressUpdate
from app.modules.audit.service import ClientInfo

router = APIRouter(prefix="/addresses", tags=["addresses"])


@router.get("", response_model=list[AddressRead])
def list_addresses(current_user: CurrentUser, db: DbSession):
    """The caller's address book, default first."""
    return service.list_addresses(db, current_user)


@router.post("", response_model=AddressRead, status_code=status.HTTP_201_CREATED)
def create_address(
    payload: AddressCreate, request: Request, current_user: CurrentUser, db: DbSession
):
    return service.create_address(
        db,
        user=current_user,
        client=ClientInfo.from_request(request),
        **payload.model_dump(),
    )


@router.patch("/{address_id}", response_model=AddressRead)
def update_address(
    address_id: uuid.UUID,
    payload: AddressUpdate,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
):
    """Change some of an address. Omitted fields are left as they were."""
    return service.update_address(
        db,
        user=current_user,
        address_id=address_id,
        client=ClientInfo.from_request(request),
        **payload.model_dump(exclude_unset=True),
    )


@router.delete("/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_address(
    address_id: uuid.UUID, request: Request, current_user: CurrentUser, db: DbSession
) -> None:
    """Remove a saved address. No placed order is affected: orders keep
    their own frozen copy of where they were sent."""
    service.delete_address(
        db,
        user=current_user,
        address_id=address_id,
        client=ClientInfo.from_request(request),
    )
