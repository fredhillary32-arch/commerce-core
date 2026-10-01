"""Shipping-rule HTTP endpoints. Administrative only.

No DELETE, for the same reason as tax rates: a rule that priced an order
stays readable beside it. Retiring one means clearing ``is_active``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.modules.audit.service import ClientInfo
from app.modules.shipping import service
from app.modules.shipping.dependencies import DbSession, ShippingManager
from app.modules.shipping.schemas import (
    ShippingRuleCreate,
    ShippingRuleRead,
    ShippingRuleUpdate,
)

router = APIRouter(prefix="/shipping-rules", tags=["shipping"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


@router.get("", response_model=list[ShippingRuleRead])
def list_shipping_rules(
    admin: ShippingManager, db: DbSession, limit: Limit = 50, offset: Offset = 0
):
    return service.list_rules(db, limit=limit, offset=offset)


@router.post("", response_model=ShippingRuleRead, status_code=status.HTTP_201_CREATED)
def create_shipping_rule(
    payload: ShippingRuleCreate, request: Request, admin: ShippingManager, db: DbSession
):
    return service.create_rule(
        db, actor=admin, client=ClientInfo.from_request(request), **payload.model_dump()
    )


@router.patch("/{rule_id}", response_model=ShippingRuleRead)
def update_shipping_rule(
    rule_id: uuid.UUID,
    payload: ShippingRuleUpdate,
    request: Request,
    admin: ShippingManager,
    db: DbSession,
):
    """Amend a rule, or retire it with ``is_active: false``."""
    return service.update_rule(
        db,
        rule_id=rule_id,
        actor=admin,
        client=ClientInfo.from_request(request),
        **payload.model_dump(exclude_unset=True),
    )
