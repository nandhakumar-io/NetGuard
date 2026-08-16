from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models.user import UserRole


class AdminUserCreate(BaseModel):
    """Unlike auth.UserCreate (public /auth/register, which forcibly
    downgrades NETWORK_ADMIN/SECURITY -- see UserCreate.sanitized_role),
    this is only ever reachable behind require_roles(NETWORK_ADMIN) (see
    POST /users), so an admin creating a teammate account can grant any
    role directly instead of the new user needing a separate role-elevation
    step immediately after their first login."""

    email: EmailStr
    full_name: str
    password: str
    role: UserRole = UserRole.NETWORK_ENGINEER


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminUserRead(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    mfa_enabled: bool
    sso_provider: str | None = None
    created_at: datetime | None = None
    last_login_at: datetime | None = None


class UserRoleCounts(BaseModel):
    total: int
    network_admin: int
    network_engineer: int
    noc_engineer: int
    security: int
    auditor: int
    disabled: int


class AdminUserListResponse(BaseModel):
    users: list[AdminUserRead]
    counts: UserRoleCounts
