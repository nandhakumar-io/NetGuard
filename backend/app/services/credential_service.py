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

from app.models.device import Device


class CredentialNotFoundError(Exception):
    """Raised when a device has no resolvable credential in the secret store."""


def _env_key(credential_ref: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "_", credential_ref.strip()).upper()
    return f"NETGUARD_CRED_{slug}"


def _fetch_from_env(credential_ref: str) -> str | None:
    return os.environ.get(_env_key(credential_ref))


def get_ssh_password(device: Device) -> str:
    """Resolve the SSH password for a device from the secret store.

    Raises CredentialNotFoundError (rather than silently deploying with an
    empty/wrong password) if the device has no `ssh_credential_ref` set, or
    if that reference doesn't resolve to anything in the store -- a missing
    credential should fail loudly before we ever open a connection to a
    production network device.
    """
    if not device.ssh_credential_ref:
        raise CredentialNotFoundError(
            f"Device '{device.hostname}' has no ssh_credential_ref configured; "
            "cannot retrieve a credential to deploy with."
        )

    password = _fetch_from_env(device.ssh_credential_ref)
    if not password:
        raise CredentialNotFoundError(
            f"No credential found in the secret store for ref "
            f"'{device.ssh_credential_ref}' (device '{device.hostname}')."
        )
    return password


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