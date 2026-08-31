"""Symmetric encryption for secrets stored directly in the database.

Used for every `*_encrypted` column in the schema: git-sync tokens,
wireless AP SNMP credentials, SMTP passwords, backup-destination
credentials, and stored configuration text (ConfigSnapshot.*_config_
encrypted, GoldenConfig.config_encrypted, ComplianceBaseline.config_
encrypted) all use the functions in this top section (encrypt/decrypt/
rotate_ciphertext), keyed by SECRET_ENCRYPTION_KEY(S).

The six Device SSH/SNMP/gNMI credential columns (ssh_password_encrypted,
ssh_private_key_encrypted, ssh_private_key_passphrase_encrypted,
gnmi_password_encrypted, snmp_community_encrypted, snmp_auth_key_
encrypted, snmp_priv_key_encrypted) use a SEPARATE key and separate
functions further down this file (encrypt_device_credential/decrypt_
device_credential/rotate_device_credential_ciphertext), keyed by
DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) -- see that section's docstring for
why: docker-compose.yaml only injects that key into `device-gateway`
(the one process that actually opens device connections), not into
`api` or the Celery workers, so an API RCE cannot decrypt device
credentials even though the general key above is still shared broadly.

All encrypted at rest with Fernet (AES-128-CBC + HMAC, via the
`cryptography` package -- already a dependency here for JWT signing).

Key versioning (applies to both scopes independently)
------------------------------------------------------
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

# Separate dev-only fallback for the device-credential scope (below) --
# deliberately a different constant from _DEV_ONLY_FALLBACK_KEY so that a
# fresh checkout with nothing configured still has genuinely distinct
# keys per scope, matching production shape, rather than both scopes
# silently colliding on the same fallback value.
_DEV_ONLY_DEVICE_CREDENTIAL_FALLBACK_KEY = "K3n2FQ8H6X1v5pYVQvQKz8yqzq2G9dJmR3wYT7bC1dQ="


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


# ---------------------------------------------------------------------
# Device-credential scope (Section 4 hardening follow-up)
# ---------------------------------------------------------------------
# A second, independent Fernet key hierarchy for exactly the six
# `Device.*_encrypted` SSH/SNMP columns (see credential_service.py):
# ssh_password, ssh_private_key, ssh_private_key_passphrase,
# snmp_community, snmp_auth_key, snmp_priv_key.
#
# Why a second key rather than reusing the one above: SECRET_ENCRYPTION_
# KEY(S) is loaded into every service via the shared .env (git sync
# tokens, wireless AP credentials, SMTP passwords, backup-destination
# credentials all use encrypt()/decrypt() above too) -- so an `api`
# container RCE could decrypt every device SSH/SNMP credential in the
# fleet even though, by design, DEVICE_GATEWAY_ENABLED=True means the
# `api` process is never supposed to need them. Splitting device
# credentials onto DEVICE_CREDENTIAL_ENCRYPTION_KEY(S), which
# docker-compose.yaml injects ONLY into `device-gateway` (plus `migrate`,
# transiently, for the one-time backfill migration), means the `api`
# container structurally cannot decrypt these six columns even under
# RCE, regardless of what application code does or doesn't call. See
# Alembic migration 0121 for the one-time re-encryption of existing rows
# from the general key to this one.
def _configured_device_credential_keys() -> list[str]:
    keys: list[str] = []
    if settings.DEVICE_CREDENTIAL_ENCRYPTION_KEY:
        keys.append(settings.DEVICE_CREDENTIAL_ENCRYPTION_KEY.strip())
    if settings.DEVICE_CREDENTIAL_ENCRYPTION_KEYS:
        for k in settings.DEVICE_CREDENTIAL_ENCRYPTION_KEYS.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    if not keys:
        keys.append(_DEV_ONLY_DEVICE_CREDENTIAL_FALLBACK_KEY)
    return keys


@lru_cache(maxsize=1)
def _device_credential_multi_fernet() -> MultiFernet:
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in _configured_device_credential_keys()])


def device_credential_active_key_count() -> int:
    return len(_configured_device_credential_keys())


def device_credential_key_configured() -> bool:
    """True only if a REAL DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) is set --
    as opposed to _configured_device_credential_keys() silently falling
    back to the hardcoded dev-only key. Callers that might run outside
    the Device Gateway process (e.g. secrets_rotation_service, which the
    `api` process's /security/secrets/rotate endpoint can trigger) MUST
    check this before calling rotate_device_credential_ciphertext --
    otherwise a rotation run from `api` (which no longer holds the real
    key by design) would silently re-encrypt every device credential
    under the dev fallback key instead of the Gateway's real key,
    corrupting fleet-wide device access while reporting a "successful"
    rotation."""
    return bool(settings.DEVICE_CREDENTIAL_ENCRYPTION_KEY or settings.DEVICE_CREDENTIAL_ENCRYPTION_KEYS)


def encrypt_device_credential(plaintext: str) -> str:
    """Same contract as encrypt(), scoped to DEVICE_CREDENTIAL_ENCRYPTION_
    KEY(S). Use only for the six Device SSH/SNMP columns -- see module
    note above."""
    return _device_credential_multi_fernet().encrypt(plaintext.encode()).decode()


def decrypt_device_credential(ciphertext: str) -> str | None:
    """Same contract as decrypt(), scoped to DEVICE_CREDENTIAL_ENCRYPTION_
    KEY(S)."""
    try:
        return _device_credential_multi_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def rotate_device_credential_ciphertext(ciphertext: str) -> str | None:
    """Same contract as rotate_ciphertext(), scoped to
    DEVICE_CREDENTIAL_ENCRYPTION_KEY(S)."""
    try:
        return _device_credential_multi_fernet().rotate(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
