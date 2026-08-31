"""Database Backups: on-demand pg_dump of the NetGuard application
database itself (users, devices, change history, audit log, ...), tracked
in BackupJob rows so the Backups page has real history rather than only a
"click to run" button with no memory of what happened last time.

Scope note: this is deliberately separate from
app.services.snapshot_service, which backs up *device configurations*
(the running-config text pulled from a router/switch). This module backs
up NetGuard's own state -- "if this server's disk died right now, could
the whole install be restored" -- which snapshot_service doesn't cover at
all.

Shells out to `pg_dump` via subprocess rather than a Python-side dump
library, same rationale as git_sync_service's `git` subprocess calls: the
official client binary is authoritative for wire-format compatibility
across Postgres versions, and a subprocess call with an explicit timeout
keeps sandboxing simple.
"""
from __future__ import annotations

import datetime
import gzip
import json
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.backup_destination import BackupDestination
from app.models.backup_job import BackupJob
from app.services import audit_service, backup_destination_service, notification_service

logger = logging.getLogger(__name__)


class BackupError(Exception):
    """Raised when a backup could not be started at all (e.g. pg_dump
    missing, DATABASE_URL unparseable) -- distinct from a BackupJob row
    that completes with status="failed", which is for a dump that started
    but errored partway through."""


def _pg_dump_target() -> dict:
    """Parses settings.DATABASE_URL (a SQLAlchemy URL, e.g.
    postgresql+psycopg2://user:pass@host:5432/dbname) into the discrete
    pieces pg_dump wants on argv, since pg_dump doesn't accept a
    SQLAlchemy-style +driver DSN directly."""
    parsed = urlsplit(settings.DATABASE_URL.replace("postgresql+psycopg2", "postgresql"))
    if not parsed.hostname or not parsed.path.lstrip("/"):
        raise BackupError("DATABASE_URL is not a valid Postgres connection string")
    return {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username) if parsed.username else "postgres",
        "password": unquote(parsed.password) if parsed.password else "",
        "dbname": parsed.path.lstrip("/"),
    }


def _prune_old_backups(db: Session) -> None:
    """Deletes both the row and the on-disk file for completed backups
    beyond BACKUP_RETENTION_COUNT, oldest first. Only ever called right
    after a successful completion (see run_database_backup) -- a failed
    run never triggers pruning, so a bad backup can't push a good one out
    of retention.
    """
    if settings.BACKUP_RETENTION_COUNT <= 0:
        return
    completed = (
        db.query(BackupJob)
        .filter(BackupJob.status == "completed")
        .order_by(BackupJob.started_at.desc())
        .all()
    )
    for stale in completed[settings.BACKUP_RETENTION_COUNT:]:
        if stale.file_path:
            try:
                Path(stale.file_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to delete pruned backup file %s", stale.file_path, exc_info=True)
        db.delete(stale)
    db.commit()


def _reconcile_stuck_jobs(db: Session) -> None:
    """Marks any BackupJob still "running" long after it should have
    finished (2x the configured pg_dump timeout, as a safety margin) as
    failed. Needed for jobs created before a crash that never reached
    _fail_job/completion -- e.g. the AttributeError this module used to
    raise on every run (see BACKUP_PGDUMP_TIMEOUT_SECONDS/
    BACKUP_RETENTION_COUNT in app.core.config) left rows stuck on
    "running" indefinitely, with no local process left to ever finish
    them. Cheap (indexed status filter) so it's safe to call on every
    GET /backups rather than needing a separate sweep job.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=settings.BACKUP_PGDUMP_TIMEOUT_SECONDS * 2
    )
    stuck = db.query(BackupJob).filter(BackupJob.status == "running").all()
    for job in stuck:
        started_at = job.started_at
        if started_at is None:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=datetime.timezone.utc)
        if started_at < cutoff:
            job.status = "failed"
            job.error_message = "Backup did not complete (server likely restarted or crashed mid-run)."
            job.completed_at = datetime.datetime.now(datetime.timezone.utc)
            job.duration_seconds = int((job.completed_at - started_at).total_seconds())
    if stuck:
        db.commit()


def run_database_backup(db: Session, *, triggered_by_email: str) -> BackupJob:
    """Runs pg_dump against the NetGuard application database, gzips the
    output, and records the result as a BackupJob row. Never raises for a
    dump failure (pg_dump missing binary, auth failure, timeout) -- those
    are captured on the row as status="failed" so the caller (POST
    /backups/database) can still return 200 with the row showing what went
    wrong, matching the Backups page's existing "failed" rows in the
    archive history rather than surfacing a 500. Only raises BackupError
    for a config problem that means no attempt could even be started.
    """
    target = _pg_dump_target()

    storage_dir = Path(settings.BACKUP_STORAGE_DIR)
    storage_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.datetime.now(datetime.timezone.utc)
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    dest_path = storage_dir / f"netguard-db-{timestamp}.sql.gz"

    job = BackupJob(id=uuid.uuid4(), status="running", triggered_by=triggered_by_email, started_at=started_at)
    db.add(job)
    db.commit()
    db.refresh(job)

    env = {"PGPASSWORD": target["password"]} if target["password"] else {}
    cmd = [
        "pg_dump",
        "-h", target["host"],
        "-p", target["port"],
        "-U", target["user"],
        "-d", target["dbname"],
        "--no-password",
        "--format=plain",
    ]

    try:
        with gzip.open(dest_path, "wb") as gz_out:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=settings.BACKUP_PGDUMP_TIMEOUT_SECONDS,
                check=True,
            )
            gz_out.write(result.stdout)

        completed_at = datetime.datetime.now(datetime.timezone.utc)
        job.status = "completed"
        job.file_path = str(dest_path)
        job.size_bytes = dest_path.stat().st_size
        job.completed_at = completed_at
        job.duration_seconds = int((completed_at - started_at).total_seconds())
        db.commit()

        audit_service.record_event(
            db, actor=triggered_by_email, action="Database Backup Completed", result="Success",
            detail=f"{dest_path.name} ({job.size_bytes} bytes)",
        )
        _upload_to_destinations(db, job, dest_path)
        _prune_old_backups(db)

    except FileNotFoundError:
        _fail_job(db, job, dest_path, "pg_dump is not installed on this server.")
    except subprocess.TimeoutExpired:
        _fail_job(db, job, dest_path, f"pg_dump timed out after {settings.BACKUP_PGDUMP_TIMEOUT_SECONDS}s.")
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"").decode(errors="replace")[-1000:]
        _fail_job(db, job, dest_path, stderr_tail or "pg_dump exited with a non-zero status.")
    except Exception as exc:  # noqa: BLE001 - deliberately broad: anything else
        # (storage_dir not writable, disk full mid-gzip-write, a DB hiccup
        # committing the "completed" row, etc.) must still resolve this job
        # to "failed" rather than leave it stuck on "running" forever with
        # nothing in the UI to explain why -- that's exactly what a narrower
        # except list (pg_dump-specific errors only) was doing: an
        # unanticipated exception here used to escape past this function
        # entirely, bubble up as a raw 500 ("Failed to start backup" with no
        # detail), and abandon the job row mid-flight since nothing ever
        # flipped its status off "running".
        logger.exception("Unexpected error during database backup")
        _fail_job(db, job, dest_path, f"Unexpected error: {exc}")

    db.refresh(job)
    return job


def _fail_job(db: Session, job: BackupJob, dest_path: Path, error_message: str) -> None:
    dest_path.unlink(missing_ok=True)  # don't leave a truncated/empty .sql.gz around
    completed_at = datetime.datetime.now(datetime.timezone.utc)
    job.status = "failed"
    job.error_message = error_message
    job.completed_at = completed_at
    started_at = job.started_at
    if started_at is not None:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=datetime.timezone.utc)
        job.duration_seconds = int((completed_at - started_at).total_seconds())
    db.commit()

    logger.warning("Database backup failed: %s", error_message)
    audit_service.record_event(
        db, actor=job.triggered_by or "system", action="Database Backup Failed", result="Failed",
        detail=error_message[:500],
    )
    notification_service.notify(
        event="Database Backup Failed",
        message=error_message[:500],
        severity="critical",
    )


def _upload_to_destinations(db: Session, job: BackupJob, local_path: Path) -> None:
    """Pushes the just-completed local backup to every enabled
    BackupDestination (S3 / Azure Blob / SFTP -- see
    app.services.backup_destination_service), recording a per-destination
    outcome on job.offsite_results. Never raises and never flips the
    job's own status/error_message: the *local* backup already succeeded
    by the time this runs (see run_database_backup), and a broken off-site
    copy shouldn't retroactively mark a good local backup as "failed" --
    it's surfaced separately (offsite_results, plus each destination's own
    last_run_status/last_error) so the Backups page can show "local: OK,
    off-site: 1 failed" instead of one conflated status.
    """
    destinations = db.query(BackupDestination).filter(BackupDestination.enabled.is_(True)).all()
    if not destinations:
        return

    results = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for dest in destinations:
        config = backup_destination_service.decrypt_config(dest.config_encrypted)
        entry = {
            "destination_id": str(dest.id),
            "name": dest.name,
            "type": dest.type,
            "status": "success",
            "error": None,
        }
        try:
            backup_destination_service.upload_to_destination(dest.type, config, local_path, local_path.name)
            dest.last_run_status = "success"
            dest.last_error = None
        except backup_destination_service.DestinationError as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)[:500]
            dest.last_run_status = "failed"
            dest.last_error = str(exc)[:500]
            logger.warning("Off-site backup upload to %s (%s) failed: %s", dest.name, dest.type, exc)
            notification_service.notify(
                event="Off-Site Backup Upload Failed",
                message=f"{job.file_path.rsplit('/', 1)[-1] if job.file_path else 'Backup'} failed to reach "
                        f"'{dest.name}' ({dest.type}): {str(exc)[:300]}",
                severity="warning",
            )
        dest.last_run_at = now
        results.append(entry)

    job.offsite_results = json.dumps(results)
    db.commit()

    failed = [r for r in results if r["status"] == "failed"]
    audit_service.record_event(
        db, actor=job.triggered_by or "system", action="Off-Site Backup Upload",
        result="Success" if not failed else "Partial Failure",
        detail=f"{len(results) - len(failed)}/{len(results)} destination(s) succeeded"
               + (f"; failed: {', '.join(r['name'] for r in failed)}" if failed else ""),
    )


def delete_backup(db: Session, job: BackupJob) -> None:
    """Removes both the on-disk file (if any) and the BackupJob row for a
    manual delete from the Backups page's archive history."""
    if job.file_path:
        Path(job.file_path).unlink(missing_ok=True)
    db.delete(job)
    db.commit()


# ---------------------------------------------------------------------------
# Device configuration backups
#
# Everything above this point is NetGuard's *own* database backup. The
# functions below are the device-config counterpart the Backups page also
# surfaces: an on-demand read of each managed device's live running config,
# persisted as an app.models.snapshot.ConfigSnapshot -- the exact same
# storage the pre-deployment auto-snapshot and rollback history already use
# (see app.api.config_management.backup_config, which now just calls
# run_device_config_backup so there's one code path for "take a config
# backup of this device" regardless of whether it was triggered from the
# device's own Config tab or the fleet-wide Backups page).
# ---------------------------------------------------------------------------


class DeviceBackupError(Exception):
    """Raised when a single device's config couldn't be read for backup --
    caught per-device by run_fleet_config_backup so one unreachable device
    doesn't abort the rest of a bulk run."""


def _push_device_config_to_destinations(
    db: Session, device, snapshot, operator_email: str, destination_ids: list | None = None
) -> None:
    """Pushes one just-taken device config snapshot to every enabled
    BackupDestination, same off-site copy every database backup already
    gets (see _upload_to_destinations) -- device config backups were
    previously never forwarded anywhere, only written to the encrypted
    ConfigSnapshot row in NetGuard's own DB, so a destination configured
    on the Backups page silently only ever received database dumps.

    Best-effort and never raises: the snapshot itself already succeeded
    and is safely on file in NetGuard's DB by the time this runs, so a
    broken off-site destination shouldn't be reported as the backup
    having failed -- same posture as _upload_to_destinations. Shares
    each BackupDestination's last_run_at/last_run_status/last_error
    fields with the database-backup path rather than adding a parallel
    per-snapshot tracking column, since the Backups page already reads
    those to show "last run" on the destination itself.
    """
    from app.services import snapshot_service

    query = db.query(BackupDestination).filter(BackupDestination.enabled.is_(True))
    if destination_ids is not None:
        # Caller asked for a specific subset (or none at all -- "local
        # only") rather than the default "push to every enabled
        # destination" behavior. An empty list here means "skip
        # off-site entirely", not "unfiltered".
        if not destination_ids:
            return
        query = query.filter(BackupDestination.id.in_(destination_ids))
    destinations = query.all()
    if not destinations:
        return

    try:
        raw_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
    except Exception:
        logger.warning("Could not decrypt snapshot %s for off-site push", snapshot.id, exc_info=True)
        return

    remote_filename = f"{device.hostname}-{snapshot.version}.cfg"
    now = datetime.datetime.now(datetime.timezone.utc)
    failures = []

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as tmp:
        tmp.write(raw_config)
        tmp_path = Path(tmp.name)

    try:
        for dest in destinations:
            config = backup_destination_service.decrypt_config(dest.config_encrypted)
            try:
                backup_destination_service.upload_to_destination(dest.type, config, tmp_path, remote_filename)
                dest.last_run_status = "success"
                dest.last_error = None
            except backup_destination_service.DestinationError as exc:
                dest.last_run_status = "failed"
                dest.last_error = str(exc)[:500]
                failures.append(f"{dest.name}: {str(exc)[:200]}")
                logger.warning(
                    "Off-site push of device config backup (%s) to %s (%s) failed: %s",
                    device.hostname, dest.name, dest.type, exc,
                )
            dest.last_run_at = now
        db.commit()
    finally:
        tmp_path.unlink(missing_ok=True)

    if failures:
        notification_service.notify(
            event="Off-Site Device Config Backup Upload Failed",
            message=f"{device.hostname} config backup ({snapshot.version}) failed to reach: " + "; ".join(failures),
            severity="warning",
        )
        audit_service.record_event(
            db, actor=operator_email, action="Off-Site Device Config Backup Upload",
            result="Partial Failure", device_hostname=device.hostname,
            detail=f"snapshot={snapshot.id}; failed: {', '.join(f.split(':')[0] for f in failures)}",
        )


def run_device_config_backup(
    db: Session, device, operator_email: str, label: str | None = None, destination_ids: list | None = None
):
    """Reads `device`'s live running config over its configured protocol and
    persists it as a new ConfigSnapshot. Raises DeviceBackupError on any
    read failure -- callers decide whether that's a hard stop (single-device
    backup) or just one failed row in a fleet-wide sweep."""
    from app.core.config import settings
    from app.models.snapshot import ConfigSnapshot
    from app.services import audit_service as _audit_service
    from app.services import device_job_service, snapshot_service
    from app.services.device_job_service import (
        DeviceJobFailedError,
        DeviceJobTimeoutError,
        DeviceOperation,
    )
    from app.services.protocol_manager import ProtocolManager

    # Section 3/9: was an unconditional in-process ProtocolManager call
    # with no DEVICE_GATEWAY_ENABLED gate (one of four found in the
    # hardening audit) -- routed through the Gateway now, matching the
    # pattern used by view_running_config in app/api/config_management.py.
    # backup_config() combines a running-config read with a best-effort
    # startup-config read; the Gateway only exposes them as two separate
    # job operations, so mirror that here (startup failure stays
    # non-fatal, same as ProtocolManager.backup_config's own behavior).
    if settings.DEVICE_GATEWAY_ENABLED:
        try:
            running_result = device_job_service.submit_job_sync(
                tenant_id=str(device.tenant_id),
                device_id=str(device.id),
                operation=DeviceOperation.GET_RUNNING_CONFIG,
                params={},
                requested_by=operator_email,
            )
        except (DeviceJobTimeoutError, DeviceJobFailedError) as exc:
            raise DeviceBackupError(getattr(exc, "error", None) or str(exc)) from exc

        running_output = running_result.output
        startup_output = None
        try:
            startup_result = device_job_service.submit_job_sync(
                tenant_id=str(device.tenant_id),
                device_id=str(device.id),
                operation=DeviceOperation.GET_STARTUP_CONFIG,
                params={},
                requested_by=operator_email,
            )
            startup_output = startup_result.output
        except (DeviceJobTimeoutError, DeviceJobFailedError):
            pass  # best-effort, same as ProtocolManager.backup_config

        result_output, result_startup_config = running_output, startup_output
    else:
        pm = ProtocolManager(db, device, operator=operator_email)
        result = pm.backup_config()
        if not result.success:
            raise DeviceBackupError(result.error or "Failed to read device configuration for backup")
        result_output, result_startup_config = result.output, result.startup_config

    version = str(int(datetime.datetime.utcnow().timestamp()))
    payload_dict = snapshot_service.build_snapshot_payload(result_output, result_startup_config, version)
    snapshot = ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **payload_dict)
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    _audit_service.record_event(
        db,
        actor=operator_email,
        action="Configuration Backup",
        result="Success",
        device_hostname=device.hostname,
        detail=f"snapshot={snapshot.id} version={snapshot.version} label={label or 'manual backup'}",
    )
    _push_device_config_to_destinations(db, device, snapshot, operator_email, destination_ids=destination_ids)
    return snapshot


def device_backup_summary(db: Session, tenant_id=None) -> list[dict]:
    """One row per managed device for the Backups page's Device Config
    Backups panel: latest snapshot's timestamp/version/checksum, a running
    count of how many backups are on file, and how many days it's been
    since the last one -- so an operator can see at a glance which devices
    haven't been backed up recently (or ever) without opening each one's
    Config tab individually.

    `tenant_id`: None (MSP staff) sees every device, same as before this
    param existed. Set for a tenant-scoped caller -- restricts the panel
    to that tenant's own devices, matching the join-based scoping used
    throughout app.api.devices/app.api.change_requests (no tenant_id
    column on ConfigSnapshot, so this only needs to filter the Device
    query itself; the snapshot lookups below are already keyed by
    device_id).

    `never_backed_up` devices sort first, then oldest-last-backup-first --
    the devices most in need of attention naturally bubble to the top
    instead of the operator having to hunt for them alphabetically.
    """
    from sqlalchemy import func as _func

    from app.models.device import Device
    from app.models.snapshot import ConfigSnapshot

    device_query = db.query(Device)
    if tenant_id is not None:
        device_query = device_query.filter(Device.tenant_id == tenant_id)
    devices = device_query.order_by(Device.hostname).all()
    latest_by_device: dict = {}
    count_by_device: dict = {}
    for device_id, count in db.query(ConfigSnapshot.device_id, _func.count(ConfigSnapshot.id)).group_by(
        ConfigSnapshot.device_id
    ):
        count_by_device[device_id] = count
    # One query per device for "the latest row" would be N+1 on a fleet
    # page -- instead pull every snapshot's (device_id, created_at, id)
    # ordered so the first time we see a device_id is its newest row.
    for snap in db.query(ConfigSnapshot).order_by(ConfigSnapshot.device_id, ConfigSnapshot.seq.desc()).all():
        latest_by_device.setdefault(snap.device_id, snap)

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for device in devices:
        latest = latest_by_device.get(device.id)
        days_since = None
        if latest and latest.created_at:
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            days_since = (now - created).days
        rows.append(
            {
                "device_id": device.id,
                "hostname": device.hostname,
                "ip_address": device.ip_address,
                "vendor": device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
                "backup_count": count_by_device.get(device.id, 0),
                "last_backup_at": latest.created_at if latest else None,
                "last_backup_version": latest.version if latest else None,
                "last_backup_snapshot_id": latest.id if latest else None,
                "days_since_backup": days_since,
            }
        )
    rows.sort(
        key=lambda r: (
            r["days_since_backup"] is not None,  # False (never backed up) sorts before True
            -(r["days_since_backup"] or -1),
        )
    )
    return rows


def run_fleet_config_backup(
    db: Session, operator_email: str, device_ids: list | None = None, destination_ids: list | None = None,
    tenant_id=None,
) -> dict:
    """Bulk "back up every device's config right now" sweep for the Backups
    page's fleet-wide button -- device_ids=None means every managed device
    (or, for a tenant-scoped caller, every device *of that tenant*; see
    `tenant_id`). Each device is independently best-effort (an unreachable
    device just lands in `failed`, same as a single manual backup failing
    wouldn't stop anyone else's), so one down switch never blocks the rest
    of the fleet from getting a fresh backup.

    `tenant_id`: None (MSP staff) reaches every device, same as before this
    param existed. Set for a tenant-scoped caller -- both the "back up
    everything" default and an explicit `device_ids` list are filtered to
    that tenant's own devices, so a tenant admin can't fleet-backup (or
    probe the existence of) another tenant's devices by ID.
    """
    from app.models.device import Device

    query = db.query(Device)
    if tenant_id is not None:
        query = query.filter(Device.tenant_id == tenant_id)
    if device_ids:
        query = query.filter(Device.id.in_(device_ids))
    devices = query.all()

    succeeded: list[str] = []
    failed: dict[str, str] = {}
    for device in devices:
        try:
            run_device_config_backup(db, device, operator_email, label="fleet backup", destination_ids=destination_ids)
            succeeded.append(device.hostname)
        except DeviceBackupError as exc:
            failed[device.hostname] = str(exc)
        except Exception as exc:  # pragma: no cover - defensive, mirrors bulk_device_action's catch-all
            failed[device.hostname] = str(exc)

    from app.services import audit_service as _audit_service

    _audit_service.record_event(
        db,
        actor=operator_email,
        action="Fleet Configuration Backup",
        result="Success" if not failed else ("Partial Failure" if succeeded else "Failure"),
        detail=f"{len(succeeded)}/{len(devices)} device(s) backed up"
        + (f"; failed: {', '.join(failed.keys())}" if failed else ""),
    )
    return {"succeeded": succeeded, "failed": failed, "total": len(devices)}
