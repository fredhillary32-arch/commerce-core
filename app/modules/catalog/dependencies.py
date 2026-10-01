"""Shared dependency aliases for the catalogue routers.

Authorization is declared here once per permission rather than repeated on
each route, so a missing guard on a new admin endpoint is visible as an
obviously different signature.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.permissions import CATALOG_READ, CATALOG_WRITE, IMAGES_WRITE, require_permission
from app.db.models.user import User
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]

CatalogReader = Annotated[User, Depends(require_permission(CATALOG_READ))]
CatalogWriter = Annotated[User, Depends(require_permission(CATALOG_WRITE))]
ImageWriter = Annotated[User, Depends(require_permission(IMAGES_WRITE))]
