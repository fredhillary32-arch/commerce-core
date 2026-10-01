"""Effective-permission resolution and the route guard built on it.

Permissions are resolved from the database on every authorized request rather
than cached in the access token, so revoking a role or adding a DENY takes
effect on the next request instead of at the next token expiry.
"""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from app.core.exceptions import AuthorizationError
from app.db.models.rbac import ALLOW, Permission, RolePermission, UserPermissionOverride, UserRole
from app.db.models.user import User
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentUser

RBAC_MANAGE = "rbac.manage"

# Catalogue administration. Seeded by migration 0005 and granted to roles by
# an RBAC administrator; no role holds them by default.
CATALOG_READ = "catalog.read"
CATALOG_WRITE = "catalog.write"
IMAGES_WRITE = "images.write"

# Inventory administration. Seeded by migration 0007, also granted to no
# role. Reading stock levels never implies changing them, so there is no
# inventory.write that would collapse the two.
INVENTORY_READ = "inventory.read"
INVENTORY_ADJUST = "inventory.adjust"

# Order administration. Seeded by migration 0009, again granted to no role.
# None of the four implies another: looking up a customer's order is not
# licence to cancel it, and tax configuration is a finance job while
# shipping configuration is an operations one.
#
# There is no customer-facing order code. Checkout, reading one's own orders
# and managing one's own addresses are authentication-only with ownership as
# the authorization, for the same reason the cart has no code: zero roles by
# default would otherwise 403 every new buyer on their own order.
ORDERS_READ = "orders.read"
ORDERS_MANAGE = "orders.manage"
TAX_MANAGE = "tax.manage"
SHIPPING_MANAGE = "shipping.manage"


def has_permission(db: Session, user_id: uuid.UUID, permission_code: str) -> bool:
    """Apply the precedence: user DENY, then user ALLOW, then role grant, then deny."""
    override = db.execute(
        select(UserPermissionOverride.effect)
        .join(Permission, Permission.id == UserPermissionOverride.permission_id)
        .where(UserPermissionOverride.user_id == user_id, Permission.code == permission_code)
    ).scalar_one_or_none()

    if override is not None:
        return override == ALLOW

    granted = db.execute(
        select(literal(1))
        .select_from(UserRole)
        .join(RolePermission, RolePermission.role_id == UserRole.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(UserRole.user_id == user_id, Permission.code == permission_code)
        .limit(1)
    ).first()

    return granted is not None


def require_permission(permission_code: str) -> Callable[..., User]:
    """Build a dependency that admits only users holding ``permission_code``."""

    def dependency(current_user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> User:
        if not has_permission(db, current_user.id, permission_code):
            raise AuthorizationError(
                "You do not have permission to perform this action.", code="PERMISSION_DENIED"
            )
        return current_user

    return dependency
