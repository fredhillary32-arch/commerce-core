from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.db.models.user import User
from app.db.session import get_db
from app.modules.auth import repository

# auto_error=False so a missing header reaches us as None and turns into the
# project's structured error body instead of FastAPI's default shape.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None:
        raise AuthenticationError(
            "Authentication is required.", code="AUTHORIZATION_REQUIRED"
        )

    claims = decode_access_token(credentials.credentials)

    user = repository.get_user_by_id(db, claims.user_id)
    if user is None:
        raise AuthenticationError(
            "Access token is missing, expired or invalid.", code="INVALID_ACCESS_TOKEN"
        )

    # Checked per request, not left to token expiry, so disabling an account
    # takes effect immediately.
    if not user.is_active:
        raise AuthenticationError("This account is disabled.", code="ACCOUNT_DISABLED")

    if claims.auth_version != user.authorization_version:
        raise AuthenticationError(
            "Authorization has changed since this token was issued; sign in again.",
            code="STALE_AUTHORIZATION",
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
