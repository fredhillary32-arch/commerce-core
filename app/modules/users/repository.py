import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.rbac import Permission, Role, RolePermission, UserPermissionOverride, UserRole


def get_role_by_id(db: Session, role_id: uuid.UUID) -> Role | None:
    return db.get(Role, role_id)


def get_role_by_name(db: Session, name: str) -> Role | None:
    return db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()


def list_roles(db: Session) -> list[Role]:
    return list(db.execute(select(Role).order_by(Role.name)).scalars())


def add_role(db: Session, *, name: str, description: str | None) -> Role:
    role = Role(name=name, description=description)
    db.add(role)
    db.flush()
    return role


def get_permission_by_code(db: Session, code: str) -> Permission | None:
    return db.execute(select(Permission).where(Permission.code == code)).scalar_one_or_none()


def get_role_permission(
    db: Session, role_id: uuid.UUID, permission_id: uuid.UUID
) -> RolePermission | None:
    return db.get(RolePermission, (role_id, permission_id))


def add_role_permission(db: Session, *, role_id: uuid.UUID, permission_id: uuid.UUID) -> None:
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))


def get_user_role(db: Session, user_id: uuid.UUID, role_id: uuid.UUID) -> UserRole | None:
    return db.get(UserRole, (user_id, role_id))


def add_user_role(db: Session, *, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    db.add(UserRole(user_id=user_id, role_id=role_id))


def get_override(
    db: Session, user_id: uuid.UUID, permission_id: uuid.UUID
) -> UserPermissionOverride | None:
    return db.get(UserPermissionOverride, (user_id, permission_id))


def add_override(
    db: Session, *, user_id: uuid.UUID, permission_id: uuid.UUID, effect: str
) -> None:
    db.add(
        UserPermissionOverride(user_id=user_id, permission_id=permission_id, effect=effect)
    )
