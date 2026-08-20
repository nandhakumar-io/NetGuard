from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.core.security import (
    create_access_token,
    create_mfa_challenge_token,
    decode_mfa_challenge_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.schemas.auth import (
    LoginRequest,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaRequiredResponse,
    MfaSetupResponse,
    MfaVerifyRequest,
    RefreshRequest,
    SessionRead,
    Token,
    UserCreate,
    UserRoleUpdate,
)
from app.services import mfa_service, rate_limiter, session_device

router = APIRouter(prefix="/auth", tags=["auth"])

# The refresh token is never returned in a JSON body and never touches
# frontend JS/localStorage -- it's set as an httpOnly cookie so that an XSS
# bug anywhere else in the app (a compromised dependency, a future
# dangerouslySetInnerHTML, etc.) cannot read it and mint long-lived sessions.
# Scoped to the auth path prefix so it isn't sent on every other API call.
REFRESH_COOKIE_NAME = "netguard_refresh_token"
_REFRESH_COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def _set_refresh_cookie(response: Response, raw_refresh: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
        max_age=60 * 60 * 24 * settings.REFRESH_TOKEN_EXPIRE_DAYS,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)


def _issue_token_pair(
    db: Session,
    user: User,
    response: Response,
    request: Request | None = None,
    rotate_record: RefreshToken | None = None,
) -> Token:
    """Issues a fresh access/refresh pair.

    `rotate_record`, when given, is the RefreshToken row being rotated by
    POST /auth/refresh: it's updated in place (new hash, new expiry) rather
    than being revoked with a brand-new row inserted alongside it. This
    keeps a session's identity (its row id, and therefore its position in
    Security > Active Sessions) stable across the silent refreshes every
    page load triggers -- previously each one minted a new "session" for
    the same browser/IP, so a handful of reloads made it look like several
    different devices had logged in. login()/mfa_verify() never pass this
    (a fresh login is legitimately a new session).

    The access token carries the session row's id as `sid` so a revoked
    session (DELETE /auth/sessions/{id}) stops authenticating immediately
    instead of only once the access token would have expired anyway -- see
    get_current_user's sid check.
    """
    raw_refresh = generate_refresh_token()
    user_agent = request.headers.get("user-agent") if request else None
    ip_address = session_device.client_ip(request) if request else None

    if rotate_record is not None:
        rotate_record.token_hash = hash_refresh_token(raw_refresh)
        rotate_record.expires_at = refresh_token_expiry()
        rotate_record.revoked = False
        # Refresh the device/IP fingerprint too -- a laptop that changed
        # networks between refreshes should show its current IP, not the
        # one it logged in from.
        rotate_record.user_agent = user_agent
        rotate_record.ip_address = ip_address
        session_record = rotate_record
    else:
        session_record = RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_token_expiry(),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        db.add(session_record)

    # Shared choke point for both the plain-password and post-MFA login
    # paths (login() and mfa_verify() both end here), so this is the one
    # place that needs to stamp "last login" rather than duplicating it in
    # both -- backs the User Management page's Last Login column (see
    # app.api.user_management).
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(session_record)

    access_token = create_access_token(subject=user.email, role=user.role.value, session_id=str(session_record.id))

    _set_refresh_cookie(response, raw_refresh)
    return Token(access_token=access_token)


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    # payload.role is client-controlled and this endpoint is unauthenticated
    # -- never trust it directly, or anyone can self-register as
    # network_admin. sanitized_role() downgrades NETWORK_ADMIN/SECURITY to
    # NETWORK_ENGINEER; those roles can only be granted afterward by an
    # existing admin via PATCH /auth/users/{user_id}/role.
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.sanitized_role(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=user.email, role=user.role.value)
    return Token(access_token=token)


@router.post("/login", response_model=Token | MfaRequiredResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    locked_out, retry_after = rate_limiter.is_locked_out(payload.email)
    if locked_out:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {retry_after} seconds.",
        )

    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        rate_limiter.record_failed_attempt(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Same is_active gate app.api.sso already applies for Google SSO login
    # -- previously only the SSO path actually enforced a disabled account,
    # so disabling a user via User Management (app.api.user_management)
    # would have silently done nothing for anyone still using a password.
    if user.is_active in (False, "false", "False"):
        raise HTTPException(status_code=403, detail="This account has been disabled")

    rate_limiter.reset_attempts(payload.email)

    if user.mfa_enabled == "true":
        mfa_token = create_mfa_challenge_token(subject=user.email)
        return MfaRequiredResponse(mfa_token=mfa_token)

    return _issue_token_pair(db, user, response, request)


@router.post("/mfa/verify", response_model=Token)
def mfa_verify(payload: MfaVerifyRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    try:
        claims = decode_mfa_challenge_token(payload.mfa_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="MFA challenge expired or invalid, please log in again")

    email = claims.get("sub")

    # A TOTP code is only 6 digits -- without a limiter here, an attacker
    # who already has a valid (short-lived) MFA challenge token could
    # brute-force the ~1M code space directly against this endpoint,
    # bypassing the point of MFA entirely. Shares the same
    # email-keyed Redis limiter as the password check above.
    locked_out, retry_after = rate_limiter.is_locked_out(f"mfa:{email}")
    if locked_out:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed MFA attempts. Try again in {retry_after} seconds.",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user or not mfa_service.verify_code(user.mfa_secret, payload.code):
        rate_limiter.record_failed_attempt(f"mfa:{email}")
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    rate_limiter.reset_attempts(f"mfa:{email}")
    return _issue_token_pair(db, user, response, request)


@router.post("/mfa/setup", response_model=MfaSetupResponse)
def mfa_setup(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Generates a new TOTP secret. MFA is NOT enabled yet -- the user must
    confirm possession of the authenticator app via POST /mfa/enable first.
    """
    secret = mfa_service.generate_secret()
    current_user.mfa_secret = secret
    db.commit()
    return MfaSetupResponse(secret=secret, otpauth_uri=mfa_service.provisioning_uri(secret, current_user.email))


@router.post("/mfa/enable")
def mfa_enable(payload: MfaCodeRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not current_user.mfa_secret:
        raise HTTPException(status_code=400, detail="Call /auth/mfa/setup first")
    if not mfa_service.verify_code(current_user.mfa_secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    current_user.mfa_enabled = "true"
    db.commit()
    return {"mfa_enabled": True}


@router.post("/mfa/disable")
def mfa_disable(payload: MfaDisableRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not verify_password(payload.password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect password")

    current_user.mfa_secret = None
    current_user.mfa_enabled = "false"
    db.commit()
    return {"mfa_enabled": False}


@router.post("/refresh", response_model=Token)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    payload: RefreshRequest | None = None,
):
    """Rotates a refresh token: the presented token is revoked and a new
    access/refresh pair is issued. Rotation limits the blast radius if a
    refresh token is ever leaked (it can only be used once).

    Reads the refresh token from the httpOnly cookie set at login. The
    request-body form (`payload.refresh_token`) is accepted only as a
    fallback for non-browser API clients that can't hold cookies -- the
    browser frontend never sends a body here.
    """
    from datetime import datetime, timezone

    raw_refresh = refresh_token_cookie or (payload.refresh_token if payload else None)
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="No refresh token presented")

    token_hash = hash_refresh_token(raw_refresh)
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()

    if record and record.expires_at.tzinfo is None:
        expires_at = record.expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = record.expires_at if record else None

    if not record or record.revoked or expires_at < datetime.now(timezone.utc):
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh token invalid or expired, please log in again")

    user = db.get(User, record.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")

    return _issue_token_pair(db, user, response, request, rotate_record=record)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    db: Session = Depends(get_db),
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    payload: RefreshRequest | None = None,
):
    raw_refresh = refresh_token_cookie or (payload.refresh_token if payload else None)
    if raw_refresh:
        token_hash = hash_refresh_token(raw_refresh)
        record = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        if record:
            record.revoked = True
            db.commit()
    _clear_refresh_cookie(response)


@router.patch("/users/{user_id}/role")
def update_user_role(
    user_id: str,
    payload: UserRoleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.NETWORK_ADMIN)),
):
    """The only path to granting NETWORK_ADMIN or SECURITY -- public
    /auth/register can never self-assign either (see UserCreate.sanitized_role).
    Restricted to existing network_admins.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = payload.role
    db.commit()
    return {"id": str(user.id), "email": user.email, "role": user.role.value}


@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """Lists this user's active (non-revoked, non-expired) refresh-token
    sessions -- i.e. everywhere they're currently logged in. Backs the
    Security page's session-management UI. A device/browser only shows up
    here between login and its refresh token being rotated/revoked/expired;
    the access token used to reach this endpoint has no session identity of
    its own (see RefreshToken model), so there's nothing to list once every
    refresh token for the account has lapsed.

    "Current" session is determined from the httpOnly refresh cookie on
    this request, never from a value the frontend supplies -- the raw
    refresh token isn't available to frontend JS at all (see the cookie
    helpers above), and accepting it as a query param would also have put
    it in server/proxy access logs.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    current_hash = hash_refresh_token(refresh_token_cookie) if refresh_token_cookie else None

    records = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == current_user.id, RefreshToken.revoked.is_(False))
        .order_by(RefreshToken.created_at.desc())
        .all()
    )

    sessions = []
    for r in records:
        expires_at = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            continue
        sessions.append(
            SessionRead(
                id=str(r.id),
                created_at=r.created_at,
                expires_at=expires_at,
                current=bool(current_hash and r.token_hash == current_hash),
                device=session_device.device_label(r.user_agent),
                ip_address=r.ip_address,
                location=session_device.location_label(r.ip_address),
            )
        )
    return sessions


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revokes one of the current user's own sessions (refresh tokens),
    e.g. to sign out a lost/stolen device remotely. Scoped to
    `user_id == current_user.id` so a user can never revoke someone else's
    session by guessing an id. Immediately effective: the associated
    refresh token can no longer be exchanged via POST /auth/refresh; any
    still-live access token for that session keeps working until it
    naturally expires (ACCESS_TOKEN_EXPIRE_MINUTES), same as /auth/logout.
    """
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.id == session_id, RefreshToken.user_id == current_user.id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Session not found")

    record.revoked = True
    db.commit()


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    # extra_roles/extra_permissions were previously omitted here entirely
    # -- the frontend's isAdmin()/hasPermission() (lib/auth.tsx) both read
    # off CurrentUser.extra_roles/extra_permissions, so any admin-granted
    # extra_roles or extra_permissions grant was silently invisible client
    # side even though app.core.deps.require_roles honored it on every
    # actual API call: the user could reach a page's endpoints directly
    # but the nav/UI gating (which only has this response to go on) kept
    # hiding the page and 403-styled buttons as if nothing had been
    # granted.
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role.value,
        "mfa_enabled": current_user.mfa_enabled == "true",
        "extra_roles": [r.strip() for r in (current_user.extra_roles or "").split(",") if r.strip()],
        "extra_permissions": [p.strip() for p in (current_user.extra_permissions or "").split(",") if p.strip()],
    }
