"""Automatic Configuration Snapshot service.

Captures running/startup configuration, encrypts it, and computes a checksum
before any deployment is allowed to proceed. Snapshots are immutable and are
the source of truth for the Self-Healing Rollback Engine.

NOTE: uses Fernet (symmetric encryption) for prototype purposes. In
production, keys should come from a managed secret store (e.g. Vault, AWS KMS).
"""
import hashlib
import base64

from cryptography.fernet import Fernet

from app.core.config import settings


def _get_fernet() -> Fernet:
    # Derive a 32-byte urlsafe base64 key from SECRET_KEY for the prototype.
    key_material = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(key_material)
    return Fernet(key)


def encrypt_config(raw_config: str) -> str:
    f = _get_fernet()
    return f.encrypt(raw_config.encode()).decode()


def decrypt_config(encrypted_config: str) -> str:
    f = _get_fernet()
    return f.decrypt(encrypted_config.encode()).decode()


def compute_checksum(raw_config: str) -> str:
    return hashlib.sha256(raw_config.encode()).hexdigest()


def build_snapshot_payload(running_config: str, startup_config: str | None, version: str) -> dict:
    return {
        "running_config_encrypted": encrypt_config(running_config),
        "startup_config_encrypted": encrypt_config(startup_config) if startup_config else None,
        "checksum": compute_checksum(running_config),
        "version": version,
    }
