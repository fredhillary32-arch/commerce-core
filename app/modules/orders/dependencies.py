"""Shared dependency aliases for the order routers.

Two kinds of route live in this module and the difference is visible in the
signature. Customer routes take ``CurrentUser`` and nothing else, so
ownership is the authorization and there is no identifier to tamper with.
Administrative routes take ``OrderReader`` or ``OrderManager``, and the two
are separate because looking up a customer's purchase is not licence to
void it.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.permissions import ORDERS_MANAGE, ORDERS_READ, require_permission
from app.db.models.user import User
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentUser

DbSession = Annotated[Session, Depends(get_db)]

OrderReader = Annotated[User, Depends(require_permission(ORDERS_READ))]
OrderManager = Annotated[User, Depends(require_permission(ORDERS_MANAGE))]

__all__ = ["CurrentUser", "DbSession", "OrderManager", "OrderReader"]
