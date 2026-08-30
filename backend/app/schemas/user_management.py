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


class UserTenancyUpdate(BaseModel):
    """Sets which tenant a user belongs to, or flips them to MSP staff
    (cross-tenant, no single tenant_id -- see app.models.user.User.
    is_msp_staff and app.core.deps.get_current_tenant_id). Mutually
    exclusive by construction: is_msp_staff=True always clears tenant_id
    server-side regardless of what's sent, since an MSP-staff account
    being scoped to one tenant would silently defeat the point of the
    flag (see update_user_tenancy)."""

    is_msp_staff: bool
    tenant_id: str | None = None


class AdminUserRead(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    extra_roles: list[UserRole] = []
    extra_permissions: list[str] = []
    is_active: bool
    # NOTE: this field was missing here even though app.api.user_management
    # _serialize() always passed it in -- Pydantic silently drops unknown
    # kwargs, so every response through this schema (GET /users, GET
    # /users/pending, POST /users/{id}/approve, ...) always came back with
    # is_approved absent. The frontend reads `!u.is_approved` to decide
    # whether to show "Pending Approval" and the Approve/Reject buttons, so
    # a missing field (falsy) made *every* user look pending forever --
    # including already-approved ones -- and clicking Approve on one of
    # those correctly 409s with "This account is already approved" from
    # app.api.user_management.approve_user. Restoring the field is the fix;
    # same migration-vs-model-drift shape as the User.is_approved gap
    # documented in app.models.user, just one layer further out.
    is_approved: bool
    mfa_enabled: bool
    sso_provider: str | None = None
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    is_msp_staff: bool = False
    tenant_id: str | None = None
    tenant_name: str | None = None


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


class AdminPasswordResetResponse(BaseModel):
    """One-time response for POST /users/{id}/reset-password. The plaintext
    temporary password is returned exactly once here (never stored, never
    logged) for the admin to hand to the user out of band -- there's no
    outbound-email service in this app to deliver it any other way.
    """

    temporary_password: str
    revoked_sessions: int
