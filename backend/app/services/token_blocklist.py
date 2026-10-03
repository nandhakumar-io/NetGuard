"""Token revocation blocklist backed by Redis.

Used to immediately invalidate NetGuard access tokens when Keycloak
revokes a session via backchannel logout (or when an admin revokes
a user's all-sessions from within NetGuard).

Keys stored: `netguard:revoked_jti:<jti>` with TTL = remaining token
lifetime, so Redis auto-expires entries once the token would have
expired anyway — the blocklist never grows unbounded.

Fail-open: if Redis is unavailable the check is skipped and the
access token is considered valid (same posture as the existing
RBAC guards). A Redis outage should not lock users out.
"""
from __future__ import annotations

import logging

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "netguard:revoked_jti:"
_redis_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    return _redis_client


def revoke_jti(jti: str, ttl_seconds: int) -> None:
    """Mark a JWT ID as revoked for the given TTL (seconds).

    Called from:
    - Keycloak backchannel logout (POST /sso/keycloak/logout)
    - Admin "revoke all sessions" for a user
    """
    if not jti or ttl_seconds <= 0:
        return
    try:
        _get_client().setex(f"{_KEY_PREFIX}{jti}", ttl_seconds, "1")
    except redis.RedisError as exc:
        logger.warning("token_blocklist: could not revoke jti %s: %s", jti, exc)


def is_revoked(jti: str) -> bool:
    """Returns True if the JTI is on the revocation list.

    Returns False (safe/fail-open) if Redis is unreachable.
    """
    try:
        return bool(_get_client().exists(f"{_KEY_PREFIX}{jti}"))
    except redis.RedisError as exc:
        logger.warning("token_blocklist: Redis unreachable during revocation check, allowing: %s", exc)
        return False


def revoke_all_for_user(user_email: str, ttl_seconds: int) -> None:
    """Scan + revoke all active JTIs for a user (by email prefix pattern).

    Used less frequently — prefer revoking a specific JTI when possible.
    TTL should match the max remaining access token lifetime.
    """
    try:
        r = _get_client()
        pattern = f"{_KEY_PREFIX}user:{user_email}:*"
        for key in r.scan_iter(pattern, count=100):
            r.expire(key, ttl_seconds)
    except redis.RedisError as exc:
        logger.warning("token_blocklist: could not revoke all JTIs for %s: %s", user_email, exc)
