from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
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

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
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
