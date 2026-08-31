"""Re-encrypt device credential columns under the new scoped key (Section 4 follow-up)

Revision ID: 0121
Revises: 0120
Create Date: 2026-08-30

Data-only migration (no schema change). Prior to this, all seven Device
SSH/SNMP/gNMI credential columns (ssh_password_encrypted,
ssh_private_key_encrypted, ssh_private_key_passphrase_encrypted,
gnmi_password_encrypted, snmp_community_encrypted, snmp_auth_key_encrypted,
snmp_priv_key_encrypted) were Fernet-encrypted under the same general
SECRET_ENCRYPTION_KEY(S) used for git-sync tokens, wireless AP credentials,
SMTP passwords, and backup-destination credentials -- all of which the
`api` container's own .env grants it. That meant an `api` RCE could
decrypt every stored device credential in the fleet even though, by
design (DEVICE_GATEWAY_ENABLED=True), the `api` process is never supposed
to need them.

This migration decrypts each of those seven columns' existing values with
the OLD general key (app.core.crypto.decrypt) and re-encrypts them with
the NEW device-credential-only key (app.core.crypto.encrypt_device_
credential). See app/core/crypto.py's "Device-credential scope" section
and app/services/credential_service.py, which now reads/writes these
columns exclusively via the new scoped functions.

REQUIRES BOTH KEYS to be present in the environment this migration runs
in: SECRET_ENCRYPTION_KEY(S) (to decrypt existing values) AND
DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) (to re-encrypt them). This is only
ever true for the `migrate` one-shot container -- docker-compose.yaml
intentionally does NOT give the long-running `api` container the new key
(that's the entire point), so this migration would silently no-op (every
row already-non-null gets logged as skipped, not corrupted -- see
skip-and-warn behavior below) if it were ever run from a process that
only has one of the two keys. It is idempotent: re-running it against
already-migrated rows will fail to decrypt under the (now retired) old
key's fallback position and each row is left untouched with a warning,
rather than double-encrypting or corrupting data.
"""
import logging

import sqlalchemy as sa

from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_DEVICE_CREDENTIAL_COLUMNS = [
    "ssh_password_encrypted",
    "ssh_private_key_encrypted",
    "ssh_private_key_passphrase_encrypted",
    "gnmi_password_encrypted",
    "snmp_community_encrypted",
    "snmp_auth_key_encrypted",
    "snmp_priv_key_encrypted",
]


def upgrade() -> None:
    from app.core import crypto  # local import: keep alembic env import-light otherwise

    if not crypto.device_credential_key_configured():
        logger.warning(
            "0121_device_credential_key_rescope: DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) is not "
            "set in this process -- skipping re-encryption entirely rather than encrypting "
            "under the dev-only fallback key. Existing device credentials remain on the OLD "
            "general key until this migration is re-run with both SECRET_ENCRYPTION_KEY(S) "
            "and DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) set (e.g. from the `migrate` container)."
        )
        return

    bind = op.get_bind()
    cols_sql = ", ".join(["id", *_DEVICE_CREDENTIAL_COLUMNS])
    rows = bind.execute(sa.text(f"SELECT {cols_sql} FROM devices")).fetchall()

    migrated = 0
    skipped_null = 0
    failed = []

    for row in rows:
        row = row._mapping
        updates = {}
        for col in _DEVICE_CREDENTIAL_COLUMNS:
            old_ciphertext = row[col]
            if old_ciphertext is None:
                skipped_null += 1
                continue

            plaintext = crypto.decrypt(old_ciphertext)
            if plaintext is None:
                # Either already migrated (encrypted under the new key,
                # which the OLD general-key decrypt() won't recognize --
                # expected on a re-run) or genuinely corrupt. Either way,
                # never overwrite -- leave it exactly as-is and flag it.
                failed.append(f"device={row['id']} column={col}")
                continue

            updates[col] = crypto.encrypt_device_credential(plaintext)

        if updates:
            set_clause = ", ".join(f"{c} = :{c}" for c in updates)
            bind.execute(
                sa.text(f"UPDATE devices SET {set_clause} WHERE id = :id"),
                {**updates, "id": row["id"]},
            )
            migrated += 1

    logger.info(
        "0121_device_credential_key_rescope: migrated %d device rows, %d null columns skipped, "
        "%d columns not re-encrypted (already-migrated or corrupt -- see failed list)",
        migrated, skipped_null, len(failed),
    )
    if failed:
        logger.warning("0121_device_credential_key_rescope: not re-encrypted: %s", failed)


def downgrade() -> None:
    # Deliberately a no-op: reversing this would mean decrypting under
    # the device-credential key and re-encrypting under the general key,
    # which only makes sense if you're also reverting the credential_
    # service.py / crypto.py code change in the same deploy. Handle that
    # as a forward-fix migration instead of a blind downgrade that could
    # silently re-widen the API's credential access.
    pass
