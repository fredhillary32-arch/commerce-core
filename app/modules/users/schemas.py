import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class RolePermissionGrant(BaseModel):
    permission_code: str = Field(min_length=1, max_length=100)


class RoleAssignment(BaseModel):
    role_name: str = Field(min_length=1, max_length=100)


class PermissionOverrideSet(BaseModel):
    permission_code: str = Field(min_length=1, max_length=100)
    # Mirrors the CHECK constraint on the table; anything else is a 422.
    effect: Literal["ALLOW", "DENY"]
