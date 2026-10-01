"""RBAC administration.

Every change that alters what a user may do bumps that user's
``authorization_version`` in the same transaction, so access tokens minted
under the old authorization state stop being accepted immediately.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.rbac import Role
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.auth import repository as auth_repository
from app.modules.users import repository


def create_role(
    db: Session, *, name: str, description: str | None, actor: User, client: ClientInfo
) -> Role:
    if repository.get_role_by_name(db, name) is not None:
        raise ConflictError(f"Role '{name}' already exists.", code="ROLE_ALREADY_EXISTS")

    role = repository.add_role(db, name=name, description=description)
    audit.record_event(
        db,
        event_type=audit.ROLE_CREATED,
        actor_user_id=actor.id,
        client=client,
        details={"role": name},
    )
    db.commit()
    db.refresh(role)
    return role


def list_roles(db: Session) -> list[Role]:
    return repository.list_roles(db)


def grant_permission_to_role(
    db: Session,
    *,
    role_id: uuid.UUID,
    permission_code: str,
    actor: User,
    client: ClientInfo,
) -> None:
    role = _require_role_by_id(db, role_id)
    permission = _require_permission(db, permission_code)

    if repository.get_role_permission(db, role.id, permission.id) is not None:
        raise ConflictError(
            f"Role '{role.name}' already grants '{permission_code}'.",
            code="PERMISSION_ALREADY_GRANTED",
        )

    repository.add_role_permission(db, role_id=role.id, permission_id=permission.id)
    audit.record_event(
        db,
        event_type=audit.ROLE_PERMISSION_GRANTED,
        actor_user_id=actor.id,
        client=client,
        details={"role": role.name, "permission": permission_code},
    )
    db.commit()


def assign_role_to_user(
    db: Session, *, user_id: uuid.UUID, role_name: str, actor: User, client: ClientInfo
) -> None:
    user = _require_user(db, user_id)
    role = _require_role_by_name(db, role_name)

    if repository.get_user_role(db, user.id, role.id) is not None:
        raise ConflictError(
            f"User already holds the '{role_name}' role.", code="ROLE_ALREADY_ASSIGNED"
        )

    repository.add_user_role(db, user_id=user.id, role_id=role.id)
    user.authorization_version += 1
    audit.record_event(
        db,
        event_type=audit.ROLE_ASSIGNED,
        actor_user_id=actor.id,
        target_user_id=user.id,
        client=client,
        details={"role": role_name},
    )
    db.commit()


def set_permission_override(
    db: Session,
    *,
    user_id: uuid.UUID,
    permission_code: str,
    effect: str,
    actor: User,
    client: ClientInfo,
) -> None:
    user = _require_user(db, user_id)
    permission = _require_permission(db, permission_code)

    existing = repository.get_override(db, user.id, permission.id)
    if existing is None:
        repository.add_override(
            db, user_id=user.id, permission_id=permission.id, effect=effect
        )
    else:
        # One row per (user, permission): flipping DENY to ALLOW updates it
        # rather than stacking a second, contradictory row.
        existing.effect = effect

    user.authorization_version += 1
    audit.record_event(
        db,
        event_type=audit.PERMISSION_OVERRIDE_SET,
        actor_user_id=actor.id,
        target_user_id=user.id,
        client=client,
        details={"permission": permission_code, "effect": effect},
    )
    db.commit()


def _require_user(db: Session, user_id: uuid.UUID) -> User:
    user = auth_repository.get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    return user


def _require_role_by_id(db: Session, role_id: uuid.UUID) -> Role:
    role = repository.get_role_by_id(db, role_id)
    if role is None:
        raise NotFoundError("Role not found.", code="ROLE_NOT_FOUND")
    return role


def _require_role_by_name(db: Session, name: str) -> Role:
    role = repository.get_role_by_name(db, name)
    if role is None:
        raise NotFoundError(f"Role '{name}' not found.", code="ROLE_NOT_FOUND")
    return role


def _require_permission(db: Session, code: str):
    permission = repository.get_permission_by_code(db, code)
    if permission is None:
        raise NotFoundError(f"Permission '{code}' not found.", code="PERMISSION_NOT_FOUND")
    return permission
