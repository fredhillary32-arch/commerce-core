"""Shared dependency aliases for the tax-rate router.

One alias, ``TaxManager``, on every route including the reads. There is
deliberately no separate ``tax.read``: a tax rate is configuration rather
than customer data, nothing on the storefront exposes the table, and adding
a second code for an audience that does not exist would be inventing
permissions nobody asked for.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.permissions import TAX_MANAGE, require_permission
from app.db.models.user import User
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]

TaxManager = Annotated[User, Depends(require_permission(TAX_MANAGE))]
