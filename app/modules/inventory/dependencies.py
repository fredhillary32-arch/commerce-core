"""Shared dependency aliases for the inventory router.

Declared once per permission rather than repeated on each route, so a
missing guard on a new endpoint is visible as an obviously different
signature. ``InventoryReader`` and ``InventoryAdjuster`` are separate
because reading stock levels never implies being allowed to change them -
there is deliberately no ``inventory.write`` that would collapse the two.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.permissions import INVENTORY_ADJUST, INVENTORY_READ, require_permission
from app.db.models.user import User
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]

InventoryReader = Annotated[User, Depends(require_permission(INVENTORY_READ))]
InventoryAdjuster = Annotated[User, Depends(require_permission(INVENTORY_ADJUST))]
