"""Password hashing, access-token signing and refresh-token secrets."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError

# argon2-cffi's defaults are Argon2id with parameters the maintainers keep in
# line with current guidance; overriding them here would be guesswork.
_password_hasher = PasswordHasher()

_REQUIRED_CLAIMS = ["sub", "iat", "exp", "jti", "auth_version"]


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Hashing a throwaway password makes a login attempt against an unknown
# address cost roughly what a real one costs, so response time does not
# disclose whether the account exists.
_DECOY_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def spend_password_verification_time() -> None:
    verify_password(_DECOY_HASH, "no-match")


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    auth_version: int
    jti: str


def create_access_token(user_id: uuid.UUID, auth_version: int) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(
            (issued_at + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()
        ),
        "jti": uuid.uuid4().hex,
        "auth_version": auth_version,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> AccessTokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": _REQUIRED_CLAIMS},
        )
        user_id = uuid.UUID(payload["sub"])
        auth_version = int(payload["auth_version"])
    except (jwt.PyJWTError, ValueError, TypeError):
        raise AuthenticationError(
            "Access token is missing, expired or invalid.", code="INVALID_ACCESS_TOKEN"
        ) from None

    return AccessTokenClaims(user_id=user_id, auth_version=auth_version, jti=payload["jti"])


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token for storage.

    SHA-256 rather than Argon2: the value is 384 bits straight from a CSPRNG,
    so there is no guessable password behind it to slow an attacker down -
    a deliberately slow hash would only add latency to every refresh.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
