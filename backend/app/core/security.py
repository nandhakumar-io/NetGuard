import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import jwt

from app.core.config import settings

# --- Password hashing ---
#
# Calls the `bcrypt` library directly rather than going through passlib's
# CryptContext. passlib 1.7.x detects the installed bcrypt version by
# reading `bcrypt.__about__.__version__`, which was removed in bcrypt
# 4.1+ -- on any environment that resolves a newer bcrypt than the
# repo's pin (a fresh `pip install` without exact version locking, a
# rebuilt image, etc.) this makes passlib silently fail to detect a
# backend and register/login start raising errors, surfaced to the user
# as a generic "Authentication failed". Calling bcrypt directly has no
# such version-sniffing step, so hashing/verifying works the same on
# every bcrypt version.
_BCRYPT_MAX_BYTES = 72  # bcrypt's own hard limit; longer inputs are truncated, matching passlib's prior default behavior


def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pw_bytes = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(pw_bytes, hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed/foreign hash format (e.g. a stale non-bcrypt hash) --
        # treat as "wrong password" rather than a 500.
        return False


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


def create_access_token(
    subject: str, role: str, expires_minutes: int | None = None, session_id: str | None = None
) -> str:
    """`session_id`, when given, is the id of the RefreshToken row this
    access token was issued alongside (see _issue_token_pair). Carried as
    the `sid` claim so get_current_user can check on every request whether
    that session has since been revoked (Security > Active Sessions ->
    Revoke) -- without it, revoking a session had no effect until the
    already-issued access token happened to expire on its own.
    """
    extra_claims = {"role": role}
    if session_id:
        extra_claims["sid"] = session_id
    return _create_token(
        subject,
        token_type="access",
        expires_delta=timedelta(minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims=extra_claims,
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


def create_sso_state_token(extra_claims: dict | None = None) -> str:
    """Short-lived, signed CSRF-protection token for the Google OIDC
    redirect round-trip (the `state` param). A JWT here rather than a
    random opaque string because it needs no server-side storage --
    the redirect to Google and back can hit different API replicas --
    while still being unguessable and tamper-evident.
    """
    return _create_token("sso-state", token_type="sso_state", expires_delta=timedelta(minutes=10), extra_claims=extra_claims)


def decode_sso_state_token(token: str) -> dict:
    payload = decode_token(token)
    if payload.get("type") != "sso_state":
        raise jwt.JWTError("Not an SSO state token")
    return payload


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
