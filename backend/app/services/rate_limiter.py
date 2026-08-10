"""Login attempt rate limiter (NFR Security: brute-force protection).

Backed by Redis (REDIS_URL -- already a hard dependency here, see
app.services.event_bus) instead of an in-memory dict, so the limit holds
across every API worker/replica rather than resetting per-process. The
previous in-memory version meant that scaling to N uvicorn workers
silently divided the effective lockout threshold by N (an attacker just
gets LOGIN_MAX_ATTEMPTS tries *per worker*, round-robined), which defeats
brute-force protection exactly when it matters most -- a production
deployment sized to handle real traffic.

Each email's failed attempts are stored as a Redis sorted set keyed by
`netguard:login_attempts:<email>`, scored by attempt timestamp, so pruning
attempts outside the lockout window is a single ZREMRANGEBYSCORE rather
than a Python-side filter. If Redis is briefly unreachable, this falls
back to an in-memory limiter for that process rather than raising (a
transient Redis blip shouldn't take down login entirely) -- but that
fallback is intentionally NOT shared across workers, so treat any
sustained Redis outage as a security-relevant incident, not just an
availability one.
"""
import logging
import time
import uuid
from collections import defaultdict
from threading import Lock

import redis

from app.core.config import settings

logger = logging.getLogger("netguard.rate_limiter")

_KEY_PREFIX = "netguard:login_attempts:"

_redis_client: redis.Redis | None = None

# Fallback only -- used solely when Redis is unreachable. Not shared
# across workers/replicas, same limitation the old implementation had.
_fallback_lock = Lock()
_fallback_attempts: dict[str, list[float]] = defaultdict(list)


def _get_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
        )
    return _redis_client


def _local_key(email: str) -> str:
    return email.strip().lower()


def _redis_key(email: str) -> str:
    return _KEY_PREFIX + _local_key(email)


def _is_locked_out_fallback(email: str) -> tuple[bool, int]:
    window_seconds = settings.LOGIN_LOCKOUT_MINUTES * 60
    with _fallback_lock:
        cutoff = time.time() - window_seconds
        attempts = [t for t in _fallback_attempts[email] if t > cutoff]
        _fallback_attempts[email] = attempts
        if len(attempts) >= settings.LOGIN_MAX_ATTEMPTS:
            retry_after = int(window_seconds - (time.time() - attempts[0]))
            return True, max(retry_after, 1)
        return False, 0


def is_locked_out(email: str) -> tuple[bool, int]:
    """Returns (locked_out, seconds_until_retry)."""
    window_seconds = settings.LOGIN_LOCKOUT_MINUTES * 60
    now = time.time()

    try:
        client = _get_client()
        key = _redis_key(email)
        client.zremrangebyscore(key, 0, now - window_seconds)
        attempts = client.zrange(key, 0, -1, withscores=True)
        if len(attempts) >= settings.LOGIN_MAX_ATTEMPTS:
            oldest_ts = attempts[0][1]
            retry_after = int(window_seconds - (now - oldest_ts))
            return True, max(retry_after, 1)
        return False, 0
    except redis.RedisError:
        logger.warning("Redis unavailable for login rate limiting, using per-process fallback", exc_info=True)
        return _is_locked_out_fallback(_local_key(email))


def record_failed_attempt(email: str) -> None:
    window_seconds = settings.LOGIN_LOCKOUT_MINUTES * 60
    now = time.time()

    try:
        client = _get_client()
        key = _redis_key(email)
        # member must be unique per entry -- score alone can collide if
        # two failures land in the same instant, so tag with a uuid.
        client.zadd(key, {f"{now}:{uuid.uuid4()}": now})
        client.expire(key, int(window_seconds) + 5)
    except redis.RedisError:
        logger.warning("Redis unavailable for login rate limiting, using per-process fallback", exc_info=True)
        with _fallback_lock:
            _fallback_attempts[_local_key(email)].append(now)


def reset_attempts(email: str) -> None:
    try:
        _get_client().delete(_redis_key(email))
    except redis.RedisError:
        logger.warning("Redis unavailable for login rate limiting, using per-process fallback", exc_info=True)
    with _fallback_lock:
        _fallback_attempts.pop(_local_key(email), None)
