import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.execute(select(User).where(User.email == email)).scalar_one_or_none()


def get_user_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def add_user(db: Session, email: str, password_hash: str) -> User:
    user = User(email=email, password_hash=password_hash)
    db.add(user)
    db.flush()
    return user


def lock_refresh_token(db: Session, token_hash: str) -> RefreshToken | None:
    """Fetch a refresh token and hold a row lock until the transaction ends.

    The lock is what makes rotation safe: two requests presenting the same
    token serialize here, so the second one sees the first one's rotation and
    is correctly treated as reuse instead of minting a second token.
    """
    return db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    ).scalar_one_or_none()


def add_refresh_token(
    db: Session,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    family_id: uuid.UUID,
    expires_at: datetime,
    created_ip: str | None,
    user_agent: str | None,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        family_id=family_id,
        expires_at=expires_at,
        created_ip=created_ip,
        user_agent=user_agent,
    )
    db.add(token)
    db.flush()
    return token


def revoke_family(db: Session, family_id: uuid.UUID, now: datetime) -> int:
    """Revoke every still-live token in one login session."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount


def revoke_all_for_user(db: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Revoke every session of a user, across all families."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount
