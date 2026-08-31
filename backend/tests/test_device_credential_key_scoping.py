"""Proves the device-credential key split (Section 4 hardening follow-up)
actually holds at runtime, not just in crypto.py's scaffolding.

This file was referenced by the hardening review as already existing with
9 passing tests -- it did not exist in the delivered archive. Written
from scratch against the real code, after finding that credential_service
was calling the *general* crypto.encrypt/decrypt for all seven Device
SSH/SNMP/gNMI columns despite DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) existing
and being wired into docker-compose.yaml -- i.e. the scoping crypto.py
implements was never actually applied, so an `api`-container RCE could
still decrypt every device credential. That call site has been fixed
alongside this test (see credential_service.py). This suite exists so
that regression can't silently reopen.
"""
import importlib

import pytest

from app.core import crypto
from app.core.config import settings
from app.models.device import Device


@pytest.fixture()
def device():
    return Device(id=1, hostname="core-router-01", tenant_id=1)


def _reset_key_caches():
    """crypto.py's MultiFernet instances are @lru_cache'd per-process, so
    tests that flip settings.DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) must
    clear the cache or they'll see a stale key list."""
    crypto._multi_fernet.cache_clear()
    crypto._device_credential_multi_fernet.cache_clear()


def _gen_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _isolate_keys(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_ENCRYPTION_KEY", _gen_key())
    monkeypatch.setattr(settings, "SECRET_ENCRYPTION_KEYS", None)
    monkeypatch.setattr(settings, "DEVICE_CREDENTIAL_ENCRYPTION_KEY", None)
    monkeypatch.setattr(settings, "DEVICE_CREDENTIAL_ENCRYPTION_KEYS", None)
    _reset_key_caches()
    yield
    _reset_key_caches()


def _set_real_device_key(monkeypatch):
    key = _gen_key()
    monkeypatch.setattr(settings, "DEVICE_CREDENTIAL_ENCRYPTION_KEY", key)
    _reset_key_caches()
    return key


# ---------------------------------------------------------------------
# 1. Scope separation at the crypto layer
# ---------------------------------------------------------------------

def test_general_and_device_scopes_use_different_keys_by_default():
    """With no DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) set, the device scope
    falls back to its own dev-only key -- distinct from the general
    scope's dev-only key -- so the two never accidentally collide even
    on a fresh checkout."""
    general_ct = crypto.encrypt("hunter2")
    device_ct = crypto.encrypt_device_credential("hunter2")
    assert general_ct != device_ct
    # Cross-scope decrypt must fail -- proves the keys are genuinely
    # independent Fernet instances, not the same key under two names.
    assert crypto.decrypt_device_credential(general_ct) is None
    assert crypto.decrypt(device_ct) is None


def test_device_scope_round_trips_under_its_own_key(monkeypatch):
    _set_real_device_key(monkeypatch)
    ct = crypto.encrypt_device_credential("s3cr3t-ssh-pass")
    assert crypto.decrypt_device_credential(ct) == "s3cr3t-ssh-pass"
    # And still cannot be read back via the general-scope function.
    assert crypto.decrypt(ct) is None


def test_device_credential_key_configured_reflects_real_setting(monkeypatch):
    assert crypto.device_credential_key_configured() is False
    _set_real_device_key(monkeypatch)
    assert crypto.device_credential_key_configured() is True


# ---------------------------------------------------------------------
# 2. credential_service round-trips through the SCOPED functions
#    (the actual bug: these previously called the general crypto.encrypt/
#    decrypt, meaning an `api`-only process could still decrypt them)
# ---------------------------------------------------------------------

def test_ssh_password_round_trip_uses_device_scope(monkeypatch, device):
    _set_real_device_key(monkeypatch)
    from app.services import credential_service

    credential_service.set_ssh_password(device, "sw1tchp@ss")
    assert device.ssh_password_encrypted is not None
    # Stored ciphertext must NOT be decryptable under the general key --
    # if it were, the scoping would be a no-op.
    assert crypto.decrypt(device.ssh_password_encrypted) is None
    assert credential_service.get_ssh_password(device) == "sw1tchp@ss"


def test_ssh_private_key_round_trip(monkeypatch, device):
    """get_ssh_private_key/set_ssh_private_key were entirely missing from
    credential_service.py despite terminal_executor.py calling them for
    key-based auth -- any key-auth terminal session would have raised
    AttributeError. Added alongside this test."""
    _set_real_device_key(monkeypatch)
    from app.services import credential_service

    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekeydata\n-----END OPENSSH PRIVATE KEY-----"
    credential_service.set_ssh_private_key(device, pem, "keypass123")
    assert crypto.decrypt(device.ssh_private_key_encrypted) is None

    got_pem, got_passphrase = credential_service.get_ssh_private_key(device)
    assert got_pem == pem
    assert got_passphrase == "keypass123"


def test_snmp_credentials_round_trip_use_device_scope(monkeypatch, device):
    _set_real_device_key(monkeypatch)
    from app.services import credential_service

    credential_service.set_snmp_credentials(
        device, community="public-ish", v3_auth_key="authkey1", v3_priv_key="privkey1"
    )
    for col in ("snmp_community_encrypted", "snmp_auth_key_encrypted", "snmp_priv_key_encrypted"):
        assert crypto.decrypt(getattr(device, col)) is None

    assert credential_service.get_snmp_community(device) == "public-ish"
    assert credential_service.get_snmp_v3_auth_key(device) == "authkey1"
    assert credential_service.get_snmp_v3_priv_key(device) == "privkey1"


def test_gnmi_password_round_trip_uses_device_scope(monkeypatch, device):
    _set_real_device_key(monkeypatch)
    from app.services import credential_service

    credential_service.set_gnmi_password(device, "gnmi-pass")
    assert crypto.decrypt(device.gnmi_password_encrypted) is None
    assert credential_service.get_gnmi_password(device) == "gnmi-pass"


# ---------------------------------------------------------------------
# 3. The corruption bug: rotation must refuse (not silently corrupt)
#    device-credential columns when run from a process without the real
#    device key (e.g. `api`'s /security/secrets/rotate).
# ---------------------------------------------------------------------

def test_rotation_refuses_device_scope_without_real_key(monkeypatch):
    """Reproduces the exact scenario from the hardening review: triggering
    rotation from a process that only holds SECRET_ENCRYPTION_KEY (like
    `api`) must not re-encrypt device-credential columns under the
    fallback dev key -- it must refuse them (reported as failed, not
    silently rotated) rather than corrupting them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base
    from app.services import secrets_rotation_service

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    # Encrypt under a real device key first (as device-gateway would),
    # then simulate running rotation from a process without that key
    # (as `api` would, by design).
    _set_real_device_key(monkeypatch)
    original_ciphertext = crypto.encrypt_device_credential("original-value")
    device = Device(hostname="core-router-01", ip_address="10.0.0.1", ssh_password_encrypted=original_ciphertext)
    db.add(device)
    db.commit()

    monkeypatch.setattr(settings, "DEVICE_CREDENTIAL_ENCRYPTION_KEY", None)
    monkeypatch.setattr(settings, "DEVICE_CREDENTIAL_ENCRYPTION_KEYS", None)
    _reset_key_caches()
    assert crypto.device_credential_key_configured() is False

    summary = secrets_rotation_service.rotate_all_secrets(db)
    device_col_result = next(r for r in summary.results if r.table == Device.__tablename__ and r.column == "ssh_password_encrypted")
    assert device_col_result.failed == 1
    assert device_col_result.rotated == 0

    db.refresh(device)
    # The stored ciphertext must be byte-for-byte unchanged -- proof
    # nothing was re-encrypted under the wrong (dev-fallback) key.
    assert device.ssh_password_encrypted == original_ciphertext


def test_stale_unscoped_rotation_module_is_not_importable_by_the_app():
    """secrets_service_rotation.py was a dead, unscoped duplicate of
    secrets_rotation_service.py that used the general key for every
    column, including device credentials -- confirmed unused by the
    hardening review but never actually deleted, leaving it as a landmine
    for a future accidental import. Removed alongside this test; this
    guards against it reappearing."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.secrets_service_rotation")
