import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class BackupJob(Base):
    """One row per database backup attempt (on-demand via POST
    /backups/database or, later, a scheduled sweep) -- see
    app.services.backup_service. Deliberately scoped to the NetGuard
    application database itself (a pg_dump), not device configs -- those
    already have their own history via app.services.snapshot_service /
    app.models.snapshot.ConfigSnapshot. "Backups" here means "can this
    NetGuard install's own state (users, devices, change history, ...) be
    restored after a disaster", which snapshot_service doesn't cover.
    """

    __tablename__ = "backup_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # "running" | "completed" | "failed" -- plain string rather than an
    # Enum column, same portability rationale as User.mfa_enabled: avoids
    # a Postgres enum-type migration if a new status is ever needed.
    status = Column(String, nullable=False, default="running")

    file_path = Column(String, nullable=True)  # absolute path under settings.BACKUP_STORAGE_DIR once completed
    size_bytes = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)  # e.g. the pg_dump stderr tail on failure

    triggered_by = Column(String, nullable=True)  # requesting user's email, or "system" for a future scheduled sweep

    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    # JSON-encoded list of per-destination off-site upload outcomes, e.g.
    # [{"destination_id": "...", "name": "Prod S3", "type": "s3",
    #   "status": "success", "error": null}, ...] -- populated by
    # app.services.backup_service after a successful local dump attempts
    # to push a copy to every enabled app.models.backup_destination.
    # BackupDestination. NULL for a job that predates this feature, or one
    # that failed before any upload was attempted, or a run with zero
    # enabled destinations configured (local-only, same as before).
    offsite_results = Column(Text, nullable=True)
