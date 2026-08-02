"""Symmetric encryption for secrets stored directly in the database.

Used by credential_service.set_snmp_credentials / get_snmp_* for SNMP
community strings and SNMPv3 auth/privacy passphrases entered through the
UI (POST /devices/{id}/snmp-credentials) -- unlike SSH credentials (which
stay in env vars only, see credential_service module docstring), the user
explicitly asked for SNMP credentials to be stored in the database for
reuse, so they're encrypted at rest with Fernet (AES-128-CBC + HMAC,
via the `cryptography` package -- already a dependency here for JWT
signing) rather than ever written as plaintext.

Key comes from SECRET_ENCRYPTION_KEY (a urlsafe-base64-encoded 32-byte
key, the same format `Fernet.generate_key()` produces). Required in
production; falls back to a fixed dev-only key so a fresh local checkout
works without extra setup -- exactly like NETGUARD_CRED_DEFAULT in
credential_service. Rotate SECRET_ENCRYPTION_KEY and every row encrypted
under the old key stops decrypting -- there's no key-versioning here, so
treat it like any other production secret: set it once via env and don't
change it without a migration plan to re-encrypt existing rows.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

# NOT for production -- see module docstring. Only used when
# SECRET_ENCRYPTION_KEY isn't set, which production deployments must set.
_DEV_ONLY_FALLBACK_KEY = "7Fn6lleogswhowuh0J7tSGtl4LVd99sW4VDJjc5XpuA="


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.SECRET_ENCRYPTION_KEY or _DEV_ONLY_FALLBACK_KEY
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    """Encrypts a secret for storage. Returns an opaque token safe to put
    in a Text column."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    """Decrypts a token produced by encrypt(). Returns None (rather than
    raising) on a bad/corrupt token or a key mismatch -- callers treat that
    the same as "no credential stored", not as a crash, since a stale row
    encrypted under a rotated-away key shouldn't take down a poll."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None