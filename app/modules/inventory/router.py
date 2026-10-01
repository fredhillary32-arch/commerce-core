"""Inventory HTTP endpoints.

Note what is absent: there is no reservation endpoint. Reservations are
created, converted, released and expired through the service layer only,
because Phase 5 checkout is their caller and an HTTP surface invented now
would simply be replaced then.

Addressed by ``variant_id`` rather than by inventory id throughout: an
operator knows which variant they are looking at, and inventory has exactly
one row per variant, so the variant is the natural handle.
"""

import uuid

from fastapi import APIRouter, Request, status

from app.modules.audit.service import ClientInfo
from app.modules.inventory import service
from app.modules.inventory.dependencies import DbSession, InventoryAdjuster, InventoryReader
from app.modules.inventory.schemas import (
    AdjustmentCreate,
    InventoryProvision,
    InventoryRead,
    ReservationRead,
    TransactionRead,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.post("", response_model=InventoryRead, status_code=status.HTTP_201_CREATED)
def provision_inventory(
    payload: InventoryProvision, request: Request, admin: InventoryAdjuster, db: DbSession
):
    """Create the zero-quantity inventory row for a variant.

    Variants that predate Phase 4 were given one by the migration backfill;
    this is how variants created since get theirs. Stock arrives afterwards
    through an adjustment, which writes a ledger entry.
    """
    inventory = service.provision_inventory(
        db,
        variant_id=payload.variant_id,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_inventory_view(inventory)


@router.get("/{variant_id}", response_model=InventoryRead)
def read_inventory(variant_id: uuid.UUID, admin: InventoryReader, db: DbSession):
    inventory = service.require_inventory_for_variant(db, variant_id)
    return service.build_inventory_view(inventory)


@router.post(
    "/{variant_id}/adjustments",
    response_model=InventoryRead,
    status_code=status.HTTP_201_CREATED,
)
def adjust_inventory(
    variant_id: uuid.UUID,
    payload: AdjustmentCreate,
    request: Request,
    admin: InventoryAdjuster,
    db: DbSession,
):
    """Correct a count, write stock off, receive stock, or move it buckets."""
    inventory = service.adjust_inventory(
        db,
        variant_id=variant_id,
        transaction_type=payload.transaction_type,
        quantity=payload.quantity,
        from_bucket=payload.from_bucket,
        to_bucket=payload.to_bucket,
        reason=payload.reason,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return service.build_inventory_view(inventory)


@router.get("/{variant_id}/transactions", response_model=list[TransactionRead])
def list_transactions(variant_id: uuid.UUID, admin: InventoryReader, db: DbSession):
    """The immutable ledger for one variant, oldest first."""
    return service.list_transactions(db, variant_id)


@router.get("/{variant_id}/reservations", response_model=list[ReservationRead])
def list_reservations(variant_id: uuid.UUID, admin: InventoryReader, db: DbSession):
    """Every hold ever placed on this stock, including terminal ones."""
    return service.list_reservations(db, variant_id)
