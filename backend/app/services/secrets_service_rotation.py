"""Re-encrypts every `*_encrypted` column in the schema under the current
primary SECRET_ENCRYPTION_KEY.

This is the operational half of key rotation described in
app.core.crypto: once a new primary key has been deployed (prepended to
SECRET_ENCRYPTION_KEY / SECRET_ENCRYPTION_KEYS, with the old key kept as
a fallback), existing rows are still encrypted under the *old* key.
`rotate_all_secrets` walks every table with an encrypted column and
re-encrypts each row's value via `crypto.rotate_ciphertext`, which uses
MultiFernet.rotate internally -- plaintext is decrypted and re-encrypted
entirely inside the `cryptography` library, so it never passes through
this module, application logs, or an intermediate variable here.

Tables covered (every `*_encrypted` Column in app.models as of this
writing):
  - Device: ssh_password_encrypted, ssh_private_key_encrypted,
    ssh_private_key_passphrase_encrypted, snmp_community_encrypted,
    snmp_auth_key_encrypted, snmp_priv_key_encrypted
  - ConfigSnapshot: running_config_encrypted, startup_config_encrypted
  - GoldenConfig: config_encrypted
  - ComplianceBaseline: config_encrypted

Transactional behavior
-----------------------
The whole rotation runs inside a single DB transaction. If any row's
ciphertext fails to validate against every configured key (already
corrupt, or encrypted under a key that's already been fully retired
from config), that row is recorded as `failed` and included in the
result -- but the row's value is left untouched (a None from
rotate_ciphertext is never written back, so a bad row never turns into
a null'd-out credential). The whole batch is committed together only if
no unexpected exception occurs; any unexpected error rolls back every
change made so far in the run, so a partial rotation never leaves some
rows on the new key and others on the old key without at least being
reported as `failed` (the expected/handled case) or fully rolled back
(the unexpected/crash case).
"""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core import crypto
from app.models.compliance_baseline import ComplianceBaseline
from app.models.device import Device
from app.models.golden_config import GoldenConfig
from app.models.snapshot import ConfigSnapshot

# (SQLAlchemy model, column attribute name) pairs for every encrypted
# column in the schema. Adding a new `*_encrypted` column anywhere else
# in the app means adding it here too -- there's no automatic discovery,
# on purpose: silently picking up an unvetted column via reflection
# could rotate something that isn't actually a Fernet token.
_ENCRYPTED_COLUMNS: list[tuple[type, str]] = [
    (Device, "ssh_password_encrypted"),
    (Device, "ssh_private_key_encrypted"),
    (Device, "ssh_private_key_passphrase_encrypted"),
    (Device, "snmp_community_encrypted"),
    (Device, "snmp_auth_key_encrypted"),
    (Device, "snmp_priv_key_encrypted"),
    (ConfigSnapshot, "running_config_encrypted"),
    (ConfigSnapshot, "startup_config_encrypted"),
    (GoldenConfig, "config_encrypted"),
    (ComplianceBaseline, "config_encrypted"),
]


@dataclass
class TableRotationResult:
    table: str
    column: str
    rotated: int = 0
    already_current: int = 0
    skipped_null: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


@dataclass
class RotationSummary:
    key_count: int
    results: list[TableRotationResult]

    @property
    def total_rotated(self) -> int:
        return sum(r.rotated for r in self.results)

    @property
    def total_failed(self) -> int:
        return sum(r.failed for r in self.results)

    def as_dict(self) -> dict:
        return {
            "active_key_count": self.key_count,
            "total_rotated": self.total_rotated,
            "total_failed": self.total_failed,
            "tables": [
                {
                    "table": r.table,
                    "column": r.column,
                    "rotated": r.rotated,
                    "skipped_null": r.skipped_null,
                    "failed": r.failed,
                    "failed_ids": r.failed_ids,
                }
                for r in self.results
            ],
        }


def rotate_all_secrets(db: Session) -> RotationSummary:
    """Re-encrypts every encrypted column, across every row, under the
    current primary key. Commits once at the end on success; the caller
    (the API endpoint) is responsible for surfacing `total_failed` to the
    operator -- failed rows are NOT an exception, since one corrupt row
    shouldn't block rotating everything else, but they DO mean that row
    is still on an old key (or unrecoverable) and needs manual follow-up.
    """
    results: list[TableRotationResult] = []

    try:
        for model, column_name in _ENCRYPTED_COLUMNS:
            result = TableRotationResult(table=model.__tablename__, column=column_name)
            rows = db.query(model).all()
            for row in rows:
                current_value = getattr(row, column_name)
                if current_value is None:
                    result.skipped_null += 1
                    continue

                new_value = crypto.rotate_ciphertext(current_value)
                if new_value is None:
                    # Never overwrite with None -- leave the row exactly
                    # as it was and flag it for manual investigation.
                    result.failed += 1
                    result.failed_ids.append(str(row.id))
                    continue

                if new_value == current_value:
                    # Extremely unlikely (Fernet tokens include a fresh
                    # IV/timestamp on every encrypt), but treat identical
                    # output as "already current" rather than counting
                    # it as a write.
                    result.already_current += 1
                else:
                    setattr(row, column_name, new_value)
                    result.rotated += 1

            results.append(result)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return RotationSummary(key_count=crypto.active_key_count(), results=results)
