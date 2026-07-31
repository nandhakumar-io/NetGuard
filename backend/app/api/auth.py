from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_mfa_challenge_token,
    decode_mfa_challenge_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.schemas.auth import (
    LoginRequest,
    Token,
    TokenPair,
    MfaRequiredResponse,
    UserCreate,
    RefreshRequest,
    MfaSetupResponse,
    MfaCodeRequest,
    MfaVerifyRequest,
    MfaDisableRequest,
)
from app.services import mfa_service, rate_limiter
from jose import JWTError

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token_pair(db: Session, user: User) -> TokenPair:
    access_token = create_access_token(subject=user.email, role=user.role.value)

    raw_refresh = generate_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=refresh_token_expiry(),
    ))
    db.commit()

    return TokenPair(access_token=access_token, refresh_token=raw_refresh)


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=user.email, role=user.role.value)
    return Token(access_token=token)


@router.post("/login", response_model=TokenPair | MfaRequiredResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
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

    rate_limiter.reset_attempts(payload.email)

    if user.mfa_enabled == "true":
        mfa_token = create_mfa_challenge_token(subject=user.email)
        return MfaRequiredResponse(mfa_token=mfa_token)

    return _issue_token_pair(db, user)


@router.post("/mfa/verify", response_model=TokenPair)
def mfa_verify(payload: MfaVerifyRequest, db: Session = Depends(get_db)):
    try:
        claims = decode_mfa_challenge_token(payload.mfa_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="MFA challenge expired or invalid, please log in again")

    user = db.query(User).filter(User.email == claims.get("sub")).first()
    if not user or not mfa_service.verify_code(user.mfa_secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    return _issue_token_pair(db, user)


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


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    """Rotates a refresh token: the presented token is revoked and a new
    access/refresh pair is issued. Rotation limits the blast radius if a
    refresh token is ever leaked (it can only be used once).
    """
    from datetime import datetime, timezone

    token_hash = hash_refresh_token(payload.refresh_token)
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()

    if record and record.expires_at.tzinfo is None:
        expires_at = record.expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = record.expires_at if record else None

    if not record or record.revoked or expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token invalid or expired, please log in again")

    record.revoked = True
    db.commit()

    user = db.get(User, record.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")

    return _issue_token_pair(db, user)


@router.post("/logout", status_code=204)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
    if record:
        record.revoked = True
        db.commit()


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role.value,
        "mfa_enabled": current_user.mfa_enabled == "true",
    }