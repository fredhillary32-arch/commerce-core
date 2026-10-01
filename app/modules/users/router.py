import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.permissions import RBAC_MANAGE, require_permission
from app.db.models.user import User
from app.db.session import get_db
from app.modules.audit.service import ClientInfo
from app.modules.auth.dependencies import CurrentUser
from app.modules.auth.schemas import UserRead
from app.modules.users import service
from app.modules.users.schemas import (
    PermissionOverrideSet,
    RoleAssignment,
    RoleCreate,
    RolePermissionGrant,
    RoleRead,
)

DbSession = Annotated[Session, Depends(get_db)]
RbacAdmin = Annotated[User, Depends(require_permission(RBAC_MANAGE))]

users_router = APIRouter(prefix="/users", tags=["users"])
roles_router = APIRouter(prefix="/roles", tags=["rbac"])


@users_router.get("/me", response_model=UserRead)
def read_current_user(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@users_router.post("/{user_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
def assign_role(
    user_id: uuid.UUID,
    payload: RoleAssignment,
    request: Request,
    admin: RbacAdmin,
    db: DbSession,
) -> None:
    service.assign_role_to_user(
        db,
        user_id=user_id,
        role_name=payload.role_name,
        actor=admin,
        client=ClientInfo.from_request(request),
    )


@users_router.post("/{user_id}/permission-overrides", status_code=status.HTTP_204_NO_CONTENT)
def set_permission_override(
    user_id: uuid.UUID,
    payload: PermissionOverrideSet,
    request: Request,
    admin: RbacAdmin,
    db: DbSession,
) -> None:
    service.set_permission_override(
        db,
        user_id=user_id,
        permission_code=payload.permission_code,
        effect=payload.effect,
        actor=admin,
        client=ClientInfo.from_request(request),
    )


@roles_router.post("", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate, request: Request, admin: RbacAdmin, db: DbSession
) -> RoleRead:
    role = service.create_role(
        db,
        name=payload.name,
        description=payload.description,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
    return RoleRead.model_validate(role)


@roles_router.get("", response_model=list[RoleRead])
def list_roles(admin: RbacAdmin, db: DbSession) -> list[RoleRead]:
    return [RoleRead.model_validate(role) for role in service.list_roles(db)]


@roles_router.post("/{role_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
def grant_permission(
    role_id: uuid.UUID,
    payload: RolePermissionGrant,
    request: Request,
    admin: RbacAdmin,
    db: DbSession,
) -> None:
    service.grant_permission_to_role(
        db,
        role_id=role_id,
        permission_code=payload.permission_code,
        actor=admin,
        client=ClientInfo.from_request(request),
    )
