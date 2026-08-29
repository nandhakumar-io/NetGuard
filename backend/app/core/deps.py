import uuid

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token, decode_push_action_token
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole

# was:  tokenUrl="auth/login"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current authenticated user from a Bearer JWT.

    Raises 401 if the token is missing/invalid/expired, or if the user no
    longer exists.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    try:
        payload = decode_access_token(token)
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Tokens issued alongside a session (see _issue_token_pair) carry that
    # session's RefreshToken row id as `sid`. If the session has since been
    # revoked -- Security > Active Sessions > Revoke, or "sign out of all
    # other sessions" -- this still-unexpired access token must stop
    # working immediately rather than staying valid until it naturally
    # expires. Tokens with no `sid` (e.g. the one-off token /auth/register
    # returns before any refresh token exists) skip this check.
    sid = payload.get("sid")
    if sid:
        try:
            session_uuid = uuid.UUID(sid)
        except ValueError:
            raise credentials_exception
        session = db.get(RefreshToken, session_uuid)
        if session is None or session.revoked:
            raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


def get_push_action_user(alert_id: uuid.UUID, action: str, token: str, db: Session) -> User:
    """Resolves the user for a mobile-push action button call (ntfy's
    `http` action hitting the API directly with a push_action bearer
    token, see security.create_push_action_token) -- deliberately
    separate from get_current_user: this never accepts a normal login
    access token, and a normal login access token never satisfies this,
    so a push_action token leaked from a notification can't be replayed
    as a real session and a stolen session token can't be used to mint
    or bypass action-button scoping.

    Raises 401 on any mismatch (wrong action, wrong alert, expired,
    tampered) or if the token's user no longer exists.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired action token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_push_action_token(token, alert_id=str(alert_id), action=action)
    except JWTError:
        raise credentials_exception
    email = payload.get("sub")
    if not email:
        raise credentials_exception
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


def get_current_user_ws(token: str, db: Session, roles: tuple[UserRole, ...] = ()) -> User | None:
    """WebSocket counterpart to get_current_user.

    WebSocket handlers can't use a normal HTTPException-raising Depends()
    for auth -- there's no HTTP response to attach a 401 to once the
    handshake has already accepted -- so every `/ws` route in this app is
    responsible for calling this *before* `websocket.accept()` and closing
    with code 1008 (Policy Violation) itself if it returns None. See
    app.api.terminal.device_terminal for the original of this pattern;
    this is the shared version so dashboard/notification/topology/alerts/
    deployments don't each carry their own copy (and don't each need their
    own reminder to actually call it -- see the incident this fixed).

    `roles`, if given, additionally requires the resolved user's *base*
    User.role to be one of them (JIT elevation is deliberately NOT
    consulted here, matching jit_access.py's approve/reject/revoke
    endpoints -- a websocket session is exactly the kind of long-lived
    grant a JIT elevation is meant to be time-boxed against, so gate it on
    the standing role only).
    """
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        email: str | None = payload.get("sub")
        if not email:
            return None
    except JWTError:
        return None

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        return None
    if roles and user.role not in roles:
        return None
    return user


def _extra_roles(user: User) -> set[str]:
    if not user.extra_roles:
        return set()
    return {r.strip() for r in user.extra_roles.split(",") if r.strip()}


def _extra_permissions(user: User) -> set[str]:
    if not user.extra_permissions:
        return set()
    return {p.strip() for p in user.extra_permissions.split(",") if p.strip()}


def has_permission(user: User, permission_key: str) -> bool:
    """True if `user` holds `permission_key` directly (via
    app.core.permissions / User.extra_permissions), or already has it
    implicitly through their base role or an extra_roles grant that
    implies it. Mirrors the frontend's lib/auth.tsx hasPermission, and is
    the check behind require_permission below.
    """
    from app.core import permissions as _permissions

    if permission_key in _extra_permissions(user):
        return True
    perm = _permissions.PERMISSION_BY_KEY.get(permission_key)
    if perm and (user.role.value in perm.implies_roles or _extra_roles(user) & set(perm.implies_roles)):
        return True
    return False


def require_roles(*roles: UserRole):
    """Dependency factory for Role-Based Access Control (RBAC).

    Usage: `Depends(require_roles(UserRole.NETWORK_ADMIN))`

    A user passes if any of the following is true:
      - their base User.role is in `roles`
      - one of their admin-granted `extra_roles` (whole other roles'
        worth of access -- see app.api.user_management, "Manage
        permissions") is in `roles`
      - one of their admin-granted `extra_permissions` (fine-grained,
        individually-grantable capability/page permissions -- see
        app.core.permissions) `implies_roles` one of `roles`. This is
        what makes granting e.g. "Configuration Management" actually
        unlock the config write endpoints it names, not just a frontend
        nav item that 403s the moment it's clicked.
      - they currently hold an active Just-In-Time elevation
        (app.services.jit_service) to one of `roles` -- see
        app.models.jit_elevation.JitElevation.

    extra_roles/extra_permissions exist for the case a user needs one
    specific other role's surface (or just one narrow capability within
    it) without a blanket promotion, or a time-boxed JIT grant when the
    need is occasional rather than standing. A base NETWORK_ADMIN never
    needs any of these -- `user.role in roles` short-circuits first, so
    an admin is never routed through JIT for their own role's endpoints.
    """

    def _check(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        role_values = {r.value for r in roles}

        if user.role in roles:
            return user

        if _extra_roles(user) & role_values:
            return user

        from app.core import permissions as _permissions

        if _permissions.implied_roles_for(_extra_permissions(user)) & role_values:
            return user

        from app.services import (
            jit_service,  # local import: avoids a deps<->services import cycle
        )

        if jit_service.active_roles_for_user(db, user.id) & role_values:
            return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role.value}' is not permitted to perform this action",
        )

    return _check


def require_permission(permission_key: str):
    """Dependency factory for a single named permission that doesn't
    necessarily imply a whole `require_roles` bucket -- e.g.
    "logs_export", which app.core.permissions deliberately gives no
    `implies_roles` since audit-log reads are already open to every
    authenticated role (see app.api.audit) and this only needs to gate a
    handful of specific export endpoints. Passes for base network_admin,
    an extra_roles grant that implies the permission, or a direct
    extra_permissions grant -- see has_permission.
    """

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role == UserRole.NETWORK_ADMIN or has_permission(user, permission_key):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission '{permission_key}'",
        )

    return _check


def require_msp_staff(user: User = Depends(get_current_user)) -> User:
    """Gate for MSP-staff-only surfaces (currently just the cross-tenant
    NOC board, app.api.tenant_board) -- deliberately checked on
    User.is_msp_staff, NOT on role, since "watches every tenant" is an
    account-level property orthogonal to a user's functional role (an
    MSP's own network_admin and their noc_engineer both need this, while
    a customer-side network_admin -- scoped to just their own tenant --
    must not get it just by holding the same role name).
    """
    if not user.is_msp_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This view is only available to MSP staff",
        )
    return user


def get_current_tenant_id(user: User = Depends(get_current_user)) -> uuid.UUID | None:
    """Returns the tenant a regular user's data access should be scoped
    to, or None for MSP staff (see User.is_msp_staff / app.models.tenant.
    Tenant) to signal "unfiltered -- every tenant". Every non-MSP user is
    expected to have tenant_id set (backfilled onto a "Default" tenant by
    migration 0092_tenants for anything that predates multi-tenancy), so
    this is the single place that "None tenant_id" gets its meaning
    decided, instead of each caller re-deriving it from is_msp_staff.
    """
    if user.is_msp_staff:
        return None
    return user.tenant_id


# Alias: some call sites (app.api.devices, app.api.alerts, app.api.
# incidents) were written against this name -- same dependency, kept as
# one alias rather than two divergent implementations so tenant-scoping
# behavior can't drift between them.
get_tenant_scope = get_current_tenant_id


def _valid_pin_step_up(token: str | None, user: User) -> bool:
    if not token:
        return False
    from jose import JWTError

    from app.core.security import decode_pin_step_up_token

    try:
        payload = decode_pin_step_up_token(token)
    except JWTError:
        return False
    return payload.get("sub") == user.email


def require_pin_step_up(
    x_pin_token: str | None = Header(default=None, alias="X-Pin-Token"),
    user: User = Depends(get_current_user),
) -> User:
    """Dependency factory... actually usable directly (not a factory) --
    drop straight onto any critical/high-blast-radius HTTP endpoint (e.g.
    device delete, config rollback) alongside its usual `require_roles`
    check: `Depends(require_pin_step_up)`.

    Only actually enforces anything for a user who both has a PIN set
    AND has opted into requiring it (User.pin_required) -- see
    app.models.user.User.security_pin_hash. For everyone else this is a
    no-op, since the PIN is an opt-in extra step-up, not a blanket new
    requirement retrofitted onto every existing account.

    When enforced, the caller must have already exchanged their PIN for
    a short-lived step-up token via POST /auth/security-pin/verify and
    sent it back as the `X-Pin-Token` header -- same shape as the
    Authorization bearer token, just proving a different, more recent
    check. See device_terminal's `_check_pin_step_up_ws` for the
    WebSocket equivalent (headers aren't available on a `/ws` upgrade
    the same way, hence the separate query-param-based check there).
    """
    if user.pin_required and user.security_pin_hash:
        if not _valid_pin_step_up(x_pin_token, user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Security PIN verification required -- verify your PIN and retry with the resulting token",
            )
    return user


def check_pin_step_up_ws(pin_token: str | None, user: User) -> bool:
    """WebSocket counterpart to require_pin_step_up -- see
    app.api.terminal.device_terminal, which calls this itself (before
    `websocket.accept()`, same convention as get_current_user_ws) rather
    than using a raising Depends(), since there's no HTTP response to
    attach a 401 to once a WS upgrade is already accepted.

    Returns True when the caller is cleared to proceed: either PIN
    step-up isn't enforced for this user at all, or a valid,
    not-yet-expired step-up token for this exact user was supplied.
    """
    if not (user.pin_required and user.security_pin_hash):
        return True
    return _valid_pin_step_up(pin_token, user)
