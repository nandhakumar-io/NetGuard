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
    # Additional roles' capabilities to grant this user on top of `role`,
    # without a blanket promotion to each one's entire surface -- see
    # app.core.deps.require_roles. E.g. a Network Engineer who also needs
    # Security's terminal-recording review: role=network_engineer,
    # extra_roles=["security"].
    extra_roles: list[UserRole] = []
    # Individually-grantable capability/page permission keys -- see
    # app.core.permissions.PERMISSION_KEYS -- for a specific narrow
    # capability (e.g. "config_management") instead of a whole extra
    # role's entire surface.
    extra_permissions: list[str] = []


class UserPermissionsUpdate(BaseModel):
    extra_roles: list[UserRole]
    # Individually-grantable capability/page permission keys -- see
    # app.core.permissions.PERMISSION_KEYS. Optional/omittable so an
    # older frontend build that only knows about extra_roles doesn't
    # accidentally wipe a user's extra_permissions on every save; the
    # endpoint replaces the set only when this field is actually sent.
    extra_permissions: list[str] | None = None


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminUserRead(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    extra_roles: list[UserRole] = []
    extra_permissions: list[str] = []
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
