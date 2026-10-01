"""Shared dependency aliases for the address router.

There is deliberately no permission alias here, exactly as in the cart
module: every route takes ``CurrentUser`` and nothing else, so a route that
tried to reach beyond the caller's own address book would have no
dependency with which to do it.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.auth.dependencies import CurrentUser

DbSession = Annotated[Session, Depends(get_db)]

__all__ = ["CurrentUser", "DbSession"]
