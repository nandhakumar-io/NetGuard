"""Login attempt rate limiter (NFR Security: brute-force protection).

Prototype implementation: in-memory, per-process. This is sufficient for a
single-worker deployment but does NOT share state across multiple API
workers/replicas. For production, back this with Redis (REDIS_URL is
already configured in settings) so limits hold across all instances --
swap the dict below for `redis.incr`/`EXPIRE` calls with the same interface.
"""
import time
from collections import defaultdict
from threading import Lock

from app.core.config import settings

_lock = Lock()
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def _key(email: str) -> str:
    return email.strip().lower()


def _prune(timestamps: list[float], window_seconds: float) -> list[float]:
    cutoff = time.time() - window_seconds
    return [t for t in timestamps if t > cutoff]


def is_locked_out(email: str) -> tuple[bool, int]:
    """Returns (locked_out, seconds_until_retry)."""
    window_seconds = settings.LOGIN_LOCKOUT_MINUTES * 60
    with _lock:
        attempts = _prune(_failed_attempts[_key(email)], window_seconds)
        _failed_attempts[_key(email)] = attempts
        if len(attempts) >= settings.LOGIN_MAX_ATTEMPTS:
            retry_after = int(window_seconds - (time.time() - attempts[0]))
            return True, max(retry_after, 1)
        return False, 0


def record_failed_attempt(email: str) -> None:
    with _lock:
        _failed_attempts[_key(email)].append(time.time())


def reset_attempts(email: str) -> None:
    with _lock:
        _failed_attempts.pop(_key(email), None)