"""Checkout and order HTTP endpoints.

Three routers rather than one, because the surface is addressed from three
roots and the split makes the authorization story legible: nothing on
``checkout_router`` or ``orders_router`` takes a permission guard, and
nothing on ``admin_orders_router`` is without one.

Note what is absent. There is **no route that confirms an order** -
``confirm_order`` is service-layer only, because Phase 6 is its only caller
and an endpoint invented now would simply be replaced then, exactly as
Phase 4 decided for ``reserve``. There is also no CRUD for orders or order
items: an order is created by checking out and is never edited.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.modules.audit.service import ClientInfo
from app.modules.orders import service
from app.modules.orders.dependencies import (
    CurrentUser,
    DbSession,
    OrderManager,
    OrderReader,
)
from app.modules.orders.schemas import CheckoutRequest, OrderRead, OrderSummary

checkout_router = APIRouter(prefix="/checkout", tags=["checkout"])
orders_router = APIRouter(prefix="/orders", tags=["orders"])
admin_orders_router = APIRouter(prefix="/admin/orders", tags=["orders-admin"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


@checkout_router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
def place_order(
    payload: CheckoutRequest,
    request: Request,
    response: Response,
    current_user: CurrentUser,
    db: DbSession,
):
    """Turn the caller's cart into an order, in a single transaction.

    Answers **201** when this call created the order and **200** when it
    found one that already existed for this cart - the retry of a submit
    whose response was lost. Either way the customer ends up with exactly
    one order, which is the guarantee ``uq_orders_cart_id`` enforces
    underneath.
    """
    order, created = service.checkout(
        db,
        user=current_user,
        shipping_address_id=payload.shipping_address_id,
        billing_address_id=payload.billing_address_id,
        client=ClientInfo.from_request(request),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return service.build_order_view(db, order)


# --------------------------------------------------------------------------
# Customer - authentication only, ownership is the authorization
# --------------------------------------------------------------------------


@orders_router.get("", response_model=list[OrderSummary])
def list_my_orders(
    current_user: CurrentUser, db: DbSession, limit: Limit = 20, offset: Offset = 0
):
    """The caller's own orders, most recent first."""
    return service.list_orders_for_user(db, current_user, limit=limit, offset=offset)


@orders_router.get("/{order_id}", response_model=OrderRead)
def read_my_order(order_id: uuid.UUID, current_user: CurrentUser, db: DbSession):
    order = service.require_order_for_user(db, order_id=order_id, user=current_user)
    return service.build_order_view(db, order)


@orders_router.post("/{order_id}/cancel", response_model=OrderRead)
def cancel_my_order(
    order_id: uuid.UUID, request: Request, current_user: CurrentUser, db: DbSession
):
    """Give up an unpaid order and get the basket back.

    Resolved through the caller's own orders first, so another customer's
    order reports 404 rather than being cancelled.
    """
    service.require_order_for_user(db, order_id=order_id, user=current_user)
    order = service.cancel_order(
        db,
        order_id=order_id,
        actor=current_user,
        client=ClientInfo.from_request(request),
    )
    return service.build_order_view(db, order)


# --------------------------------------------------------------------------
# Administrative
# --------------------------------------------------------------------------


@admin_orders_router.get("", response_model=list[OrderSummary])
def list_all_orders(
    admin: OrderReader,
    db: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
    order_status: str | None = None,
):
    return service.list_orders(db, limit=limit, offset=offset, status=order_status)


@admin_orders_router.get("/{order_id}", response_model=OrderRead)
def read_any_order(order_id: uuid.UUID, admin: OrderReader, db: DbSession):
    return service.build_order_view(db, service.require_order(db, order_id))


@admin_orders_router.post("/{order_id}/cancel", response_model=OrderRead)
def cancel_any_order(
    order_id: uuid.UUID, request: Request, admin: OrderManager, db: DbSession
):
    """Cancel any customer's unpaid order. Requires ``orders.manage``.

    ``orders.read`` is deliberately not enough: viewing an order and
    voiding one are different powers.
    """
    order = service.cancel_order(
        db, order_id=order_id, actor=admin, client=ClientInfo.from_request(request)
    )
    return service.build_order_view(db, order)
