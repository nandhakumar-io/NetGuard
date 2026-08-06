"""Automatic Configuration Snapshot service.

Captures running/startup configuration, encrypts it, and computes a checksum
before any deployment is allowed to proceed. Snapshots are immutable and are
the source of truth for the Self-Healing Rollback Engine.

NOTE: uses Fernet (symmetric encryption) for prototype purposes. In
production, keys should come from a managed secret store (e.g. Vault, AWS KMS).
"""
import datetime
import hashlib
import base64

from cryptography.fernet import Fernet
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.change_request import ChangeRequest
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot


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


def next_seq(db: Session) -> int:
    """Next value for ConfigSnapshot.seq, a monotonic tiebreaker used to
    order snapshots newest-first even when two are written in the same
    `created_at` tick (see the column docstring on the model). Not backed
    by a DB-generated identity/sequence -- Postgres identity columns can't
    be nullable, which breaks the SQLite engine the test suite uses -- so
    every caller that creates a ConfigSnapshot must call this first.
    """
    current_max = db.query(func.max(ConfigSnapshot.seq)).scalar()
    return (current_max or 0) + 1


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------
def retention_policy() -> dict:
    """The currently-configured retention policy, in the same shape callers
    (API responses, purge_expired_snapshots) both use, so the enforced
    policy and the one shown to users can never silently drift apart.
    """
    return {
        "retention_days": settings.SNAPSHOT_RETENTION_DAYS,
        "min_snapshots_per_device": settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE,
        "sweep_hour_utc": settings.SNAPSHOT_RETENTION_SWEEP_HOUR_UTC,
        "description": (
            f"Snapshots older than {settings.SNAPSHOT_RETENTION_DAYS} days are purged nightly "
            f"(~{settings.SNAPSHOT_RETENTION_SWEEP_HOUR_UTC:02d}:00 UTC), except the "
            f"{settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE} most recent snapshots for each device "
            "are always kept regardless of age, and any snapshot a rollback has restored from is "
            "never purged."
        ),
    }


def retention_status_for_device(db: Session, device_id) -> dict:
    """Per-device view of the same policy: how many snapshots exist, how
    many are protected by the min-per-device floor, and how many are
    currently eligible for the next purge sweep. Powers the "N of M kept"
    line on the Backups tab so retention isn't just a policy statement --
    it's visible per device.
    """
    policy = retention_policy()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=policy["retention_days"])

    snapshots = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.seq.desc())
        .all()
    )
    protected_ids = _protected_snapshot_ids(db, snapshots, policy["min_snapshots_per_device"])

    eligible = [s for s in snapshots if s.id not in protected_ids and _is_naive_aware(s.created_at) < cutoff]

    return {
        "device_id": device_id,
        "total_snapshots": len(snapshots),
        "protected_snapshots": len(protected_ids),
        "eligible_for_purge": len(eligible),
        "oldest_snapshot_at": snapshots[-1].created_at if snapshots else None,
        "newest_snapshot_at": snapshots[0].created_at if snapshots else None,
    }


def _is_naive_aware(dt: datetime.datetime) -> datetime.datetime:
    """SQLite (used by the test suite) doesn't round-trip timezone-aware
    datetimes, so created_at can come back naive there even though
    Postgres (production) always returns it tz-aware. Normalize to UTC
    either way before comparing against the (always tz-aware) cutoff.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _protected_snapshot_ids(db: Session, device_snapshots: list[ConfigSnapshot], min_per_device: int) -> set:
    """IDs that must never be purged for this device's snapshot list
    (already ordered newest-first): the N most recent, plus any snapshot
    a ChangeRequest.rollback_snapshot_id still points at -- that FK is
    what a rollback's audit trail ("restored from snapshot X") relies on,
    so age-based cleanup must not pull it out from under a historical
    record.
    """
    protected = {s.id for s in device_snapshots[:min_per_device]}
    if not device_snapshots:
        return protected

    referenced = (
        db.query(ChangeRequest.rollback_snapshot_id)
        .filter(
            ChangeRequest.rollback_snapshot_id.in_([s.id for s in device_snapshots]),
        )
        .all()
    )
    protected.update(row[0] for row in referenced if row[0] is not None)
    return protected


def purge_expired_snapshots(db: Session, device_id=None) -> dict:
    """Enforces the retention policy (see retention_policy()) across every
    device, or a single device if `device_id` is given. Intended to run
    nightly via Celery beat (see app.tasks.run_snapshot_retention_task /
    celery_app "snapshot-retention-sweep"), but is a plain function so it
    can also be exercised directly (tests, an on-demand admin endpoint).

    Returns {"devices_checked": int, "snapshots_deleted": int}.
    """
    policy = retention_policy()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=policy["retention_days"])

    device_ids = (
        [device_id] if device_id is not None else [d.id for d in db.query(Device.id).all()]
    )

    total_deleted = 0
    for dev_id in device_ids:
        snapshots = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == dev_id)
            .order_by(ConfigSnapshot.seq.desc())
            .all()
        )
        if not snapshots:
            continue

        protected_ids = _protected_snapshot_ids(db, snapshots, policy["min_snapshots_per_device"])
        expired = [
            s for s in snapshots if s.id not in protected_ids and _is_naive_aware(s.created_at) < cutoff
        ]
        for snap in expired:
            db.delete(snap)
        total_deleted += len(expired)

    if total_deleted:
        db.commit()

    return {"devices_checked": len(device_ids), "snapshots_deleted": total_deleted}