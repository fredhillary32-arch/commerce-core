"""Shared dependency aliases for the cart router.

There is deliberately no permission alias here, and the contrast with
``app/modules/catalog/dependencies.py`` is the point: every cart route takes
``CurrentUser`` and nothing else, so a route that tried to widen its reach
beyond the caller's own cart would have no dependency to do it with.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.auth.dependencies import CurrentUser

DbSession = Annotated[Session, Depends(get_db)]

__all__ = ["CurrentUser", "DbSession"]
