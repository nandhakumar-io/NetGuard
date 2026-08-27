"""Full-transcript recording for interactive device terminal sessions
(app.api.terminal.device_terminal) -- keystrokes in, device output out,
both timestamped -- not just the start/stop AuditLog entries that
already existed. See app.models.terminal_session_recording for why.

One recording = one JSON-Lines file on disk (settings.
TERMINAL_RECORDING_DIR/<recording id>.jsonl) plus one
TerminalSessionRecording metadata row. Writes are append-only and
synchronous-but-cheap (a handful of small writes per keystroke/output
chunk for a human-paced interactive session, nowhere near backup_
service's or snapshot_service's data volumes) -- no batching/async
queue, same reasoning as command_guard's per-line buffering: simplicity
over throughput for something that's fundamentally interactive-speed.

Secret-shaped output (e.g. a `show running-config` that echoes back
plaintext credentials) is redacted line-by-line through
secret_scan_service before it's written -- a session recording exists
to prove *what an operator did*, not to become a second place SNMP
community strings and PSKs are stored in the clear.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.terminal_session_recording import TerminalSessionRecording
from app.services import secret_scan_service


class SessionRecorder:
    """Owns one session's recording: the DB row, the open file handle,
    and elapsed-time bookkeeping. One instance per WebSocket terminal
    session -- created in app.api.terminal.device_terminal right after
    the session's audit "Opened" event, closed in a `finally` alongside
    the websocket teardown.
    """

    def __init__(self, db: Session, *, device_id, device_hostname: str, user_id, actor_email: str, jit_elevation_id=None):
        self.db = db
        self.enabled = settings.TERMINAL_SESSION_RECORDING_ENABLED
        self._started_monotonic = datetime.datetime.now(datetime.timezone.utc)
        self._file = None
        self.recording: TerminalSessionRecording | None = None

        if not self.enabled:
            return

        self.recording = TerminalSessionRecording(
            id=uuid.uuid4(), device_id=device_id, device_hostname=device_hostname,
            user_id=user_id, actor_email=actor_email, jit_elevation_id=jit_elevation_id,
        )
        db.add(self.recording)
        db.commit()
        db.refresh(self.recording)

        storage_dir = Path(settings.TERMINAL_RECORDING_DIR)
        storage_dir.mkdir(parents=True, exist_ok=True)
        file_path = storage_dir / f"{self.recording.id}.jsonl"
        self.recording.file_path = str(file_path)
        db.commit()

        self._file = open(file_path, "a", encoding="utf-8")

    def set_protocol(self, protocol: str) -> None:
        if self.recording is None:
            return
        self.recording.protocol = protocol
        self.db.commit()

    def _elapsed(self) -> float:
        return (datetime.datetime.now(datetime.timezone.utc) - self._started_monotonic).total_seconds()

    def _write(self, direction: str, data: str) -> None:
        if self._file is None or not data:
            return
        clean_data, redacted = secret_scan_service.redact_text(data)
        record = {"t": round(self._elapsed(), 3), "dir": direction, "data": clean_data}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self._file.write(line)
        self._file.flush()
        if self.recording is not None:
            self.recording.byte_count += len(line)
            if redacted and not self.recording.redacted:
                self.recording.redacted = True

    def record_input(self, data: str) -> None:
        """Browser -> device (what the operator typed)."""
        self._write("in", data)

    def record_output(self, data: str) -> None:
        """Device -> browser (what the device sent back)."""
        self._write("out", data)

    def close(self, *, reason: str | None = None) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:  # noqa: BLE001
                pass
            self._file = None
        if self.recording is not None:
            self.recording.ended_at = datetime.datetime.now(datetime.timezone.utc)
            self.recording.close_reason = reason
            try:
                self.db.commit()
            except Exception:  # noqa: BLE001
                self.db.rollback()


def read_transcript(recording: TerminalSessionRecording) -> list[dict]:
    """Reads a completed (or in-progress) recording's JSON-Lines file
    back into a list of {"t", "dir", "data"} records, for the playback/
    download endpoint. Tolerates a partially-written last line (session
    still open, or the process died mid-write) by skipping any line that
    fails to parse rather than raising.
    """
    if not recording.file_path or not Path(recording.file_path).exists():
        return []
    records: list[dict] = []
    with open(recording.file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def delete_recordings(db: Session, rows: list[TerminalSessionRecording]) -> int:
    """Deletes the given TerminalSessionRecording rows and their backing
    files -- the reviewer-initiated counterpart to purge_expired's
    scheduled sweep (same delete-file-then-delete-row mechanics), used
    by the single/bulk/all delete endpoints in api.terminal_recordings.
    Caller is responsible for authorization and audit logging; this is
    just the deletion mechanics, kept separate so all three endpoints
    share exactly one code path.
    """
    deleted = 0
    for row in rows:
        if row.file_path:
            try:
                Path(row.file_path).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(row)
        deleted += 1
    db.commit()
    return deleted


def purge_expired(db: Session) -> int:
    """Deletes TerminalSessionRecording rows (and their backing files)
    older than settings.TERMINAL_RECORDING_RETENTION_DAYS. Mirrors flow_
    service.purge_expired / snapshot retention's sweep pattern -- meant
    to be run periodically via Celery beat, not called inline anywhere
    session-critical.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=settings.TERMINAL_RECORDING_RETENTION_DAYS
    )
    rows = (
        db.query(TerminalSessionRecording)
        .filter(TerminalSessionRecording.started_at < cutoff)
        .all()
    )
    deleted = 0
    for row in rows:
        if row.file_path:
            try:
                Path(row.file_path).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(row)
        deleted += 1
    db.commit()
    return deleted
