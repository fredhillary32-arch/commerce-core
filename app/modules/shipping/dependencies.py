"""Shared dependency aliases for the shipping-rule router.

``ShippingManager`` is separate from ``TaxManager`` and neither implies the
other. In a real organisation these are different jobs - tax is finance and
compliance, shipping is operations - and a single combined code would force
whoever sets delivery prices to also be trusted with the tax table.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.permissions import SHIPPING_MANAGE, require_permission
from app.db.models.user import User
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]

ShippingManager = Annotated[User, Depends(require_permission(SHIPPING_MANAGE))]
