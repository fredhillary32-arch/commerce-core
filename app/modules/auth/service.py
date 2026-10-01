"""Authentication workflows: registration, login, refresh rotation, logout."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    spend_password_verification_time,
    verify_password,
)
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.auth import repository


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register(db: Session, *, email: str, password: str, client: ClientInfo) -> User:
    email = normalize_email(email)
    if repository.get_user_by_email(db, email) is not None:
        raise ConflictError("An account with that email already exists.", code="DUPLICATE_EMAIL")

    try:
        user = repository.add_user(db, email=email, password_hash=hash_password(password))
    except IntegrityError:
        # Another request registered the same address between the check above
        # and this insert; the unique constraint is the real arbiter.
        db.rollback()
        raise ConflictError(
            "An account with that email already exists.", code="DUPLICATE_EMAIL"
        ) from None

    audit.record_event(
        db, event_type=audit.ACCOUNT_REGISTERED, target_user_id=user.id, client=client
    )
    db.commit()
    db.refresh(user)
    return user


def login(db: Session, *, email: str, password: str, client: ClientInfo) -> IssuedTokens:
    user = repository.get_user_by_email(db, normalize_email(email))

    if user is None:
        # Equalise the cost so a missing account is not detectably faster.
        spend_password_verification_time()
        audit.record_event(
            db,
            event_type=audit.LOGIN_FAILED,
            client=client,
            details={"reason": "unknown_account"},
        )
        db.commit()
        raise _invalid_credentials()

    if not verify_password(user.password_hash, password):
        audit.record_event(
            db,
            event_type=audit.LOGIN_FAILED,
            target_user_id=user.id,
            client=client,
            details={"reason": "invalid_password"},
        )
        db.commit()
        raise _invalid_credentials()

    # Only reported once the password is proven, so it discloses nothing to
    # someone probing for accounts.
    if not user.is_active:
        audit.record_event(
            db,
            event_type=audit.LOGIN_FAILED,
            target_user_id=user.id,
            client=client,
            details={"reason": "account_disabled"},
        )
        db.commit()
        raise _account_disabled()

    tokens, _ = _issue_tokens(db, user=user, client=client, family_id=uuid.uuid4())
    audit.record_event(
        db,
        event_type=audit.LOGIN_SUCCEEDED,
        actor_user_id=user.id,
        target_user_id=user.id,
        client=client,
    )
    db.commit()
    return tokens


def refresh(db: Session, *, refresh_token: str, client: ClientInfo) -> IssuedTokens:
    now = datetime.now(timezone.utc)
    stored = repository.lock_refresh_token(db, hash_refresh_token(refresh_token))

    if stored is None:
        raise _invalid_refresh_token()

    if stored.rotated_at is not None:
        # This token was already exchanged. Either it leaked or a client is
        # replaying it; both mean the whole session chain is untrustworthy.
        repository.revoke_family(db, stored.family_id, now)
        audit.record_event(
            db,
            event_type=audit.REFRESH_TOKEN_REUSED,
            target_user_id=stored.user_id,
            client=client,
            details={"family_id": str(stored.family_id)},
        )
        db.commit()
        raise AuthenticationError(
            "Refresh token has already been used; sign in again.", code="REFRESH_TOKEN_REUSED"
        )

    if stored.revoked_at is not None or stored.expires_at <= now:
        raise _invalid_refresh_token()

    user = repository.get_user_by_id(db, stored.user_id)
    if user is None or not user.is_active:
        raise _account_disabled()

    tokens, replacement = _issue_tokens(
        db, user=user, client=client, family_id=stored.family_id
    )
    stored.rotated_at = now
    stored.replaced_by_id = replacement.id
    db.commit()
    return tokens


def logout(db: Session, *, user: User, refresh_token: str, client: ClientInfo) -> None:
    stored = repository.lock_refresh_token(db, hash_refresh_token(refresh_token))
    if stored is None or stored.user_id != user.id:
        raise _invalid_refresh_token()

    repository.revoke_family(db, stored.family_id, datetime.now(timezone.utc))
    audit.record_event(
        db,
        event_type=audit.LOGOUT,
        actor_user_id=user.id,
        target_user_id=user.id,
        client=client,
        details={"family_id": str(stored.family_id)},
    )
    db.commit()


def change_password(
    db: Session, *, user: User, current_password: str, new_password: str, client: ClientInfo
) -> None:
    if not verify_password(user.password_hash, current_password):
        raise AuthenticationError(
            "Current password is incorrect.", code="INVALID_CREDENTIALS"
        )

    user.password_hash = hash_password(new_password)
    # Every device must re-authenticate: a password change usually means the
    # old one is no longer trusted. Authorization is unchanged, so
    # authorization_version deliberately stays put.
    repository.revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
    audit.record_event(
        db,
        event_type=audit.PASSWORD_CHANGED,
        actor_user_id=user.id,
        target_user_id=user.id,
        client=client,
    )
    db.commit()


def _issue_tokens(
    db: Session, *, user: User, client: ClientInfo, family_id: uuid.UUID
) -> tuple[IssuedTokens, RefreshToken]:
    settings = get_settings()
    raw_refresh_token = generate_refresh_token()

    stored = repository.add_refresh_token(
        db,
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh_token),
        family_id=family_id,
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.refresh_token_expire_days),
        created_ip=client.ip_address,
        user_agent=client.user_agent,
    )

    tokens = IssuedTokens(
        access_token=create_access_token(
            user_id=user.id, auth_version=user.authorization_version
        ),
        refresh_token=raw_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    return tokens, stored


def _invalid_credentials() -> AuthenticationError:
    return AuthenticationError("Email or password is incorrect.", code="INVALID_CREDENTIALS")


def _account_disabled() -> AuthenticationError:
    return AuthenticationError("This account is disabled.", code="ACCOUNT_DISABLED")


def _invalid_refresh_token() -> AuthenticationError:
    return AuthenticationError(
        "Refresh token is missing, expired or invalid.", code="INVALID_REFRESH_TOKEN"
    )
