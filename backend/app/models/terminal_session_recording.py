import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class TerminalSessionRecording(Base):
    """One row per interactive device terminal session opened through
    app.api.terminal.device_terminal (SSH or Telnet, real device or the
    Demo Mode canned transcript). Metadata only -- the actual
    keystroke/output transcript lives on disk at `file_path` (JSON
    Lines, one {"t": elapsed_seconds, "dir": "in"|"out", "data": ...}
    record per chunk written), under settings.TERMINAL_RECORDING_DIR --
    same file-on-disk-plus-metadata-row pattern as BackupJob.

    Exists because audit_service already logs session *start/end*
    events ("Terminal Session Opened", "Terminal Command Blocked", etc.)
    but never captured session *content* -- which is what PCI DSS
    (10.2, remote/privileged access) and SOC 2 (CC6.1/CC6.3) reviewers
    actually ask for on a tool that hands out live CLI access to
    production network gear, doubly so for a session opened under an
    active JIT elevation (jit_elevation_id) rather than a standing role.
    """

    __tablename__ = "terminal_session_recordings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    actor_email = Column(String, nullable=False)  # denormalized, same rationale as AuditLog.actor

    # Set when the session was opened while the user held an active JIT
    # elevation (app.models.jit_elevation.JitElevation) -- lets a
    # reviewer pull "every session recorded under this specific
    # elevation" without joining through audit_log's free-text detail.
    # Null for sessions opened under a standing role.
    jit_elevation_id = Column(UUID(as_uuid=True), ForeignKey("jit_elevations.id"), nullable=True, index=True)

    protocol = Column(String, nullable=True)  # "ssh" | "telnet" | "demo" -- set once the session's protocol is known
    device_hostname = Column(String, nullable=True)  # denormalized for display without a join once device rows churn

    file_path = Column(String, nullable=True)  # absolute path under settings.TERMINAL_RECORDING_DIR
    byte_count = Column(Integer, nullable=False, default=0)
    redacted = Column(Boolean, nullable=False, default=False)  # true if a redaction pass stripped detected secrets

    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String, nullable=True)
