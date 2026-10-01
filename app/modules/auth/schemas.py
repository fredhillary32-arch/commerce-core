import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Long enough to be worth hashing, capped so an enormous body cannot turn
# Argon2 into a denial-of-service lever.
Password = Field(min_length=12, max_length=128)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Password


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Password


class UserRead(BaseModel):
    """Public view of an account. password_hash is deliberately absent."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    email_verified: bool
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
