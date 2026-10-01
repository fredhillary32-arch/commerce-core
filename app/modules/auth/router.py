from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.audit.service import ClientInfo
from app.modules.auth import service
from app.modules.auth.dependencies import CurrentUser
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: DbSession) -> UserRead:
    user = service.register(
        db,
        email=payload.email,
        password=payload.password,
        client=ClientInfo.from_request(request),
    )
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenPair:
    tokens = service.login(
        db,
        email=payload.email,
        password=payload.password,
        client=ClientInfo.from_request(request),
    )
    return TokenPair(**vars(tokens))


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, request: Request, db: DbSession) -> TokenPair:
    tokens = service.refresh(
        db, refresh_token=payload.refresh_token, client=ClientInfo.from_request(request)
    )
    return TokenPair(**vars(tokens))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest, request: Request, current_user: CurrentUser, db: DbSession
) -> None:
    service.logout(
        db,
        user=current_user,
        refresh_token=payload.refresh_token,
        client=ClientInfo.from_request(request),
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
) -> None:
    service.change_password(
        db,
        user=current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        client=ClientInfo.from_request(request),
    )
