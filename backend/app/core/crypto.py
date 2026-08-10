"""Symmetric encryption for secrets stored directly in the database.

Used for every `*_encrypted` column in the schema: SNMP credentials
(Device.snmp_*_encrypted) entered through the UI (POST
/devices/{id}/snmp-credentials), SSH credentials stored per-device
(Device.ssh_password_encrypted / ssh_private_key_encrypted /
ssh_private_key_passphrase_encrypted), and stored configuration text
(ConfigSnapshot.*_config_encrypted, GoldenConfig.config_encrypted,
ComplianceBaseline.config_encrypted). All encrypted at rest with Fernet
(AES-128-CBC + HMAC, via the `cryptography` package -- already a
dependency here for JWT signing).

Key versioning
--------------
Keys come from SECRET_ENCRYPTION_KEYS: a comma-separated, newest-first
list of urlsafe-base64-encoded 32-byte Fernet keys. `encrypt()` always
uses the *first* (primary) key. `decrypt()` tries every key in the list
in order via `cryptography.fernet.MultiFernet`, so ciphertext encrypted
under an older key keeps decrypting after a new primary key is added --
there's no separate "key id" stored alongside the ciphertext; MultiFernet
just tries each key's HMAC/timestamp in turn until one validates.

For backward compatibility, SECRET_ENCRYPTION_KEY (singular) is still
read and, if set, is prepended as the newest key ahead of anything in
SECRET_ENCRYPTION_KEYS -- existing single-key deployments keep working
with zero config changes.

To rotate:
  1. Generate a new key (`Fernet.generate_key()`), prepend it to
     SECRET_ENCRYPTION_KEYS (newest-first), keep the old key later in
     the list, and redeploy. New writes now use the new key; old rows
     still decrypt via the old key further down the list.
  2. Run the rotation job (app.services.secrets_rotation_service /
     POST /security/secrets/rotate) to re-encrypt every existing row
     under the new primary key, without ever exposing plaintext outside
     this module (see `rotate_ciphertext` below, which uses
     MultiFernet.rotate -- decrypt-then-reencrypt happens inside the
     `cryptography` library, not in application code).
  3. Once the rotation job reports zero rows still needing it, the old
     key can be dropped from SECRET_ENCRYPTION_KEYS on the next deploy.

Falls back to a fixed dev-only key so a fresh local checkout works
without extra setup -- exactly like NETGUARD_CRED_DEFAULT in
credential_service. Required in production (see
Settings.validate_production_secrets).
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings

# NOT for production -- see module docstring. Only used when no key is
# configured at all, which production deployments must not rely on.
_DEV_ONLY_FALLBACK_KEY = "7Fn6lleogswhowuh0J7tSGtl4LVd99sW4VDJjc5XpuA="


def _configured_keys() -> list[str]:
    """Newest-first list of configured Fernet keys, deduplicated but
    order-preserving (first occurrence wins), falling back to the
    dev-only key when nothing is configured."""
    keys: list[str] = []
    if settings.SECRET_ENCRYPTION_KEY:
        keys.append(settings.SECRET_ENCRYPTION_KEY.strip())
    if settings.SECRET_ENCRYPTION_KEYS:
        for k in settings.SECRET_ENCRYPTION_KEYS.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    if not keys:
        keys.append(_DEV_ONLY_FALLBACK_KEY)
    return keys


@lru_cache(maxsize=1)
def _multi_fernet() -> MultiFernet:
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in _configured_keys()])


def active_key_count() -> int:
    """How many keys are currently configured (primary + fallbacks).
    Surfaced on the rotation status endpoint so an operator can see at a
    glance whether an old key is still in play."""
    return len(_configured_keys())


def encrypt(plaintext: str) -> str:
    """Encrypts a secret for storage, always under the primary (first
    configured) key. Returns an opaque token safe to put in a Text
    column."""
    return _multi_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    """Decrypts a token produced by encrypt(), trying every configured
    key. Returns None (rather than raising) on a bad/corrupt token or a
    key mismatch against *every* configured key -- callers treat that
    the same as "no credential stored", not as a crash, since a stale
    row encrypted under a fully-retired key shouldn't take down a poll."""
    try:
        return _multi_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def rotate_ciphertext(ciphertext: str) -> str | None:
    """Re-encrypts a token under the current primary key, without the
    plaintext ever leaving the `cryptography` library (MultiFernet.rotate
    decrypts-then-reencrypts internally). Returns None if the token
    doesn't validate against any configured key (already corrupt, or
    encrypted under a key that's been fully retired) -- callers should
    treat that as "could not rotate this row" and report it rather than
    silently dropping data.

    Idempotent-ish: if the token is already under the primary key,
    MultiFernet.rotate still re-encrypts it (new IV/timestamp), which is
    harmless -- rotation is defined as "decryptable under only the
    primary key afterwards", not "byte-identical to a no-op"."""
    try:
        return _multi_fernet().rotate(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
