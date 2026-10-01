"""Tax-rate HTTP endpoints. Administrative only.

There is no DELETE. A rate that has taxed an order stays readable beside
the orders it taxed; retiring one means setting ``is_active`` false, which
also frees its jurisdiction for a replacement.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.modules.audit.service import ClientInfo
from app.modules.tax import service
from app.modules.tax.dependencies import DbSession, TaxManager
from app.modules.tax.schemas import TaxRateCreate, TaxRateRead, TaxRateUpdate

router = APIRouter(prefix="/tax-rates", tags=["tax"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


@router.get("", response_model=list[TaxRateRead])
def list_tax_rates(admin: TaxManager, db: DbSession, limit: Limit = 50, offset: Offset = 0):
    return service.list_rates(db, limit=limit, offset=offset)


@router.post("", response_model=TaxRateRead, status_code=status.HTTP_201_CREATED)
def create_tax_rate(
    payload: TaxRateCreate, request: Request, admin: TaxManager, db: DbSession
):
    return service.create_rate(
        db, actor=admin, client=ClientInfo.from_request(request), **payload.model_dump()
    )


@router.patch("/{rate_id}", response_model=TaxRateRead)
def update_tax_rate(
    rate_id: uuid.UUID,
    payload: TaxRateUpdate,
    request: Request,
    admin: TaxManager,
    db: DbSession,
):
    """Amend a rate, or retire it with ``is_active: false``."""
    return service.update_rate(
        db,
        rate_id=rate_id,
        actor=admin,
        client=ClientInfo.from_request(request),
        **payload.model_dump(exclude_unset=True),
    )
