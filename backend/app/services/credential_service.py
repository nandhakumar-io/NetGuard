"""Device credential retrieval service.

Previously the deployment pipeline hardcoded `password=""` with a comment
that real credentials "come from a secret store, not stored in DB" -- but
nothing ever actually fetched them, so every real deployment would fail
Netmiko auth. This service is that missing lookup.

Interface is intentionally tiny (one function) so swapping the backing
store is a one-file change:

  - Prototype / dev: environment variables, one per `ssh_credential_ref`,
    named `NETGUARD_CRED_<REF>` (ref is upper-cased, non-alnum -> "_").
    This keeps real passwords out of the database and out of source control
    (they live in .env / the process environment) without standing up a
    real secrets backend for local development.
  - Production: swap `_fetch_from_env` for a call to Vault, AWS Secrets
    Manager, or Azure Key Vault, keyed by the same `ssh_credential_ref`.
    Callers (pipeline_service) don't need to change.
"""
import os
import re

from app.core import crypto
from app.core.config import settings
from app.models.device import Device


class CredentialNotFoundError(Exception):
    """Raised when a device has no resolvable credential in the secret store."""


def _env_key(credential_ref: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "_", credential_ref.strip()).upper()
    return f"NETGUARD_CRED_{slug}"


def _fetch_from_env(credential_ref: str) -> str | None:
    return os.environ.get(_env_key(credential_ref))


def _fetch_dev_default() -> str | None:
    """Dev-only fallback used when a device's specific ref has no matching
    NETGUARD_CRED_<REF> entry.

    GNS3 lab bootstrap (app.api.gns3._slug_cred_ref) mints a fresh
    ssh_credential_ref per node (e.g. "gns3-ciscoiosvl2-1"), so every newly
    bootstrapped lab device needs its own env var or it 404s/500s on
    config read until someone manually adds one. Lab images conventionally
    all share one login, so in development we fall back to a single
    NETGUARD_CRED_DEFAULT instead of forcing a per-node env entry.
    Never applied outside ENVIRONMENT=development, so production behavior
    (fail loudly on an unmapped credential) is unchanged.
    """
    if settings.ENVIRONMENT != "development":
        return None
    return os.environ.get("NETGUARD_CRED_DEFAULT")


def get_ssh_password(device: Device) -> str:
    """Resolve the SSH password for a device.

    Priority (same order as the SNMP getters below): the DB-encrypted
    password set via POST /devices/{id}/ssh-credentials, then the legacy
    ssh_credential_ref -> NETGUARD_CRED_<REF> env var, then (development
    only) NETGUARD_CRED_DEFAULT.

    Raises CredentialNotFoundError (rather than silently deploying with an
    empty/wrong password) if none of those resolve to anything -- a missing
    credential should fail loudly before we ever open a connection to a
    production network device.
    """
    if device.ssh_password_encrypted:
        password = crypto.decrypt(device.ssh_password_encrypted)
        if password:
            return password

    if device.ssh_credential_ref:
        password = _fetch_from_env(device.ssh_credential_ref)
        if password:
            return password

    password = _fetch_dev_default()
    if password:
        return password

    raise CredentialNotFoundError(
        f"Device '{device.hostname}' has no SSH credential configured. "
        "Set one via POST /devices/{id}/ssh-credentials."
    )


def set_ssh_password(device: Device, password: str) -> None:
    """Encrypts and stores the SSH password directly on the device row.
    Pass "" to explicitly clear it (falls back to ssh_credential_ref / the
    dev default again). Caller is responsible for db.commit().
    """
    device.ssh_password_encrypted = crypto.encrypt(password) if password else None


def get_secret(credential_ref: str | None, *, device: Device, label: str) -> str:
    """Generic secret-store lookup for the non-SSH credential refs added
    for NETCONF/SNMP (SNMP community string, SNMP v3 auth/privacy
    passphrases). Same env-var-backed store as get_ssh_password, just not
    hardcoded to the ssh_credential_ref field so it can resolve any of the
    protocol-specific *_ref columns on Device.
    """
    if not credential_ref:
        raise CredentialNotFoundError(f"Device '{device.hostname}' has no {label} credential configured.")
    secret = _fetch_from_env(credential_ref)
    if not secret:
        raise CredentialNotFoundError(
            f"No {label} credential found in the secret store for ref '{credential_ref}' (device '{device.hostname}')."
        )
    return secret


# ---------------------------------------------------------------------
# SNMP credentials -- DB-encrypted storage (app.core.crypto), entered via
# POST /devices/{id}/snmp-credentials rather than an env-var ref. This is
# a deliberate departure from the SSH pattern above: the user explicitly
# wants SNMP credentials configurable per-device and persisted for reuse,
# not hand-added to .env one device at a time. The legacy snmp_*_ref /
# env-var path is kept as a fallback for any device that still only has
# that set (or for anyone who prefers the env-var workflow), but the
# DB-encrypted columns take priority whenever present.
# ---------------------------------------------------------------------

def set_snmp_credentials(
    device: Device,
    *,
    community: str | None = None,
    v3_auth_key: str | None = None,
    v3_priv_key: str | None = None,
) -> None:
    """Encrypts and stores SNMP secrets directly on the device row. Only
    touches fields actually passed (None = leave untouched); pass "" to
    explicitly clear a field. Caller is responsible for db.commit().
    """
    if community is not None:
        device.snmp_community_encrypted = crypto.encrypt(community) if community else None
    if v3_auth_key is not None:
        device.snmp_auth_key_encrypted = crypto.encrypt(v3_auth_key) if v3_auth_key else None
    if v3_priv_key is not None:
        device.snmp_priv_key_encrypted = crypto.encrypt(v3_priv_key) if v3_priv_key else None


def get_snmp_community(device: Device) -> str:
    """v1/v2c community string. DB-encrypted value (set via
    POST /devices/{id}/snmp-credentials) takes priority; falls back to
    the legacy env-var ref (snmp_community_ref), then the dev-only
    NETGUARD_CRED_DEFAULT, before failing loudly.
    """
    if device.snmp_community_encrypted:
        secret = crypto.decrypt(device.snmp_community_encrypted)
        if secret:
            return secret
    if device.snmp_community_ref:
        secret = _fetch_from_env(device.snmp_community_ref)
        if secret:
            return secret
    fallback = _fetch_dev_default()
    if fallback:
        return fallback
    raise CredentialNotFoundError(
        f"Device '{device.hostname}' has no SNMP community configured. "
        "Set one via POST /devices/{id}/snmp-credentials."
    )


def get_snmp_v3_auth_key(device: Device) -> str | None:
    """SNMPv3 auth passphrase, or None if not configured (valid for
    noAuthNoPriv). DB-encrypted value takes priority over the legacy
    env-var ref."""
    if device.snmp_auth_key_encrypted:
        secret = crypto.decrypt(device.snmp_auth_key_encrypted)
        if secret:
            return secret
    if device.snmp_auth_credential_ref:
        return _fetch_from_env(device.snmp_auth_credential_ref)
    return None


def get_snmp_v3_priv_key(device: Device) -> str | None:
    """SNMPv3 privacy passphrase, or None if not configured (valid for
    noAuthNoPriv/authNoPriv). DB-encrypted value takes priority over the
    legacy env-var ref."""
    if device.snmp_priv_key_encrypted:
        secret = crypto.decrypt(device.snmp_priv_key_encrypted)
        if secret:
            return secret
    if device.snmp_privacy_credential_ref:
        return _fetch_from_env(device.snmp_privacy_credential_ref)
    return None
