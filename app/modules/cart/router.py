"""Cart HTTP endpoints.

Authentication only, no permission code. Every route resolves the cart from
the authenticated user, so ownership *is* the authorization: there is no
path parameter or body field naming a cart or a user, and therefore nothing
to tamper with. See ``app.modules.cart.service`` for why a cart.* permission
would be actively wrong under the Phase 2 zero-roles rule.
"""

import uuid

from fastapi import APIRouter, Request, status

from app.modules.audit.service import ClientInfo
from app.modules.cart import service
from app.modules.cart.dependencies import CurrentUser, DbSession
from app.modules.cart.schemas import CartItemAdd, CartItemUpdate, CartRead

router = APIRouter(prefix="/cart", tags=["cart"])


@router.get("", response_model=CartRead)
def read_cart(current_user: CurrentUser, db: DbSession):
    """The caller's cart, created on first use.

    A GET that may create is unusual, but the alternative - making every
    client POST an empty cart before it can show one - puts a write on the
    happy path of simply opening the basket.
    """
    cart = service.get_or_create_active_cart(db, current_user)
    return service.build_cart_view(db, cart)


@router.post("/items", response_model=CartRead, status_code=status.HTTP_201_CREATED)
def add_item(payload: CartItemAdd, request: Request, current_user: CurrentUser, db: DbSession):
    cart = service.add_item(
        db,
        user=current_user,
        variant_id=payload.variant_id,
        quantity=payload.quantity,
        client=ClientInfo.from_request(request),
    )
    return service.build_cart_view(db, cart)


@router.patch("/items/{item_id}", response_model=CartRead)
def update_item(
    item_id: uuid.UUID,
    payload: CartItemUpdate,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
):
    """Set a line's quantity; zero removes it, which is the defined rule."""
    cart = service.update_item_quantity(
        db,
        user=current_user,
        item_id=item_id,
        quantity=payload.quantity,
        client=ClientInfo.from_request(request),
    )
    return service.build_cart_view(db, cart)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_item(
    item_id: uuid.UUID, request: Request, current_user: CurrentUser, db: DbSession
) -> None:
    service.remove_item(
        db, user=current_user, item_id=item_id, client=ClientInfo.from_request(request)
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_cart(request: Request, current_user: CurrentUser, db: DbSession) -> None:
    """Empty the cart. The cart stays ACTIVE and keeps its identity."""
    service.clear_cart(db, user=current_user, client=ClientInfo.from_request(request))
