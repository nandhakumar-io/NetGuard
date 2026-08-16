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
import logging
import subprocess
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.backup_job import BackupJob
from app.services import audit_service, notification_service

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
        _prune_old_backups(db)

    except FileNotFoundError:
        _fail_job(db, job, dest_path, "pg_dump is not installed on this server.")
    except subprocess.TimeoutExpired:
        _fail_job(db, job, dest_path, f"pg_dump timed out after {settings.BACKUP_PGDUMP_TIMEOUT_SECONDS}s.")
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"").decode(errors="replace")[-1000:]
        _fail_job(db, job, dest_path, stderr_tail or "pg_dump exited with a non-zero status.")

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


def delete_backup(db: Session, job: BackupJob) -> None:
    """Removes both the on-disk file (if any) and the BackupJob row for a
    manual delete from the Backups page's archive history."""
    if job.file_path:
        Path(job.file_path).unlink(missing_ok=True)
    db.delete(job)
    db.commit()
