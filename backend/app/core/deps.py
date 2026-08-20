from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
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
        session = db.get(RefreshToken, sid)
        if session is None or session.revoked:
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


def require_roles(*roles: UserRole):
    """Dependency factory for Role-Based Access Control (RBAC).

    Usage: `Depends(require_roles(UserRole.NETWORK_ADMIN))`

    A user passes if their base User.role is in `roles`, OR they currently
    hold an active Just-In-Time elevation (app.services.jit_service) to
    one of `roles` -- see app.models.jit_elevation.JitElevation. This is
    the one place JIT access actually takes effect: everywhere else in the
    app checks a plain User.role, unaware that it might be temporary.
    """

    def _check(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if user.role in roles:
            return user

        from app.services import (
            jit_service,  # local import: avoids a deps<->services import cycle
        )

        role_values = {r.value for r in roles}
        if jit_service.active_roles_for_user(db, user.id) & role_values:
            return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role.value}' is not permitted to perform this action",
        )

    return _check
