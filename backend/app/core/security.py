import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# --- JWT access / MFA-challenge tokens ---
#
# Every token carries a "type" claim so one kind can never be replayed as
# another (e.g. an MFA challenge token, which proves only that a password
# check passed, must not work as a bearer access token).

def _create_token(subject: str, token_type: str, expires_delta: timedelta, extra_claims: dict | None = None) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": subject, "type": token_type, "exp": expire}
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, role: str, expires_minutes: int | None = None) -> str:
    return _create_token(
        subject,
        token_type="access",
        expires_delta=timedelta(minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims={"role": role},
    )


def create_mfa_challenge_token(subject: str) -> str:
    """Short-lived token proving "password check passed" without granting
    API access. Exchanged for a real token pair via POST /auth/mfa/verify.
    """
    return _create_token(
        subject,
        token_type="mfa_challenge",
        expires_delta=timedelta(minutes=settings.MFA_CHALLENGE_EXPIRE_MINUTES),
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def decode_access_token(token: str) -> dict:
    """Decodes a token and verifies it is an access token (not an MFA
    challenge token or anything else)."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise jwt.JWTError("Not an access token")
    return payload


def decode_mfa_challenge_token(token: str) -> dict:
    payload = decode_token(token)
    if payload.get("type") != "mfa_challenge":
        raise jwt.JWTError("Not an MFA challenge token")
    return payload


# --- Refresh tokens ---
#
# Opaque random strings, not JWTs -- only their SHA-256 hash is persisted
# (see RefreshToken model), so a stolen DB dump doesn't leak usable tokens,
# and a token can be revoked (logout, rotation) without waiting for it to
# expire the way a stateless JWT would require.

def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)