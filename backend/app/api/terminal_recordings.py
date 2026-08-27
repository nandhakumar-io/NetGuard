"""Privileged terminal session recordings -- list, view transcript, and
download the raw JSON-Lines file for a session opened through
app.api.terminal.device_terminal. See app.models.terminal_session_
recording and app.services.session_recording_service for what's
actually captured and why.

Restricted to SECURITY and NETWORK_ADMIN: a transcript can contain
`show running-config` output, live troubleshooting of production gear,
and (best-effort redaction notwithstanding, see `redacted` on the
serialized row) potentially sensitive command output -- the same
access bar as the audit log and credential rotation, tighter than a
device's own Terminal button (open to NETWORK_ENGINEER/NOC_ENGINEER
too, since driving a session is a different bar than reviewing every
session ever recorded).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.terminal_session_recording import TerminalSessionRecording
from app.models.user import User, UserRole
from app.services import audit_service, session_recording_service

router = APIRouter(prefix="/terminal-recordings", tags=["terminal-recordings"])

_reviewer_only = require_roles(UserRole.SECURITY, UserRole.NETWORK_ADMIN)
# Deletion is destructive to what is, functionally, an audit artifact
# (PCI DSS 10.2 / SOC 2 CC6.1 evidence) -- narrower than the SECURITY +
# NETWORK_ADMIN bar that can merely *view* recordings, same posture as
# audit_service's own retention controls: a Network Administrator can
# review every session but shouldn't unilaterally be able to erase the
# evidence trail. SECURITY only.
_deleter_only = require_roles(UserRole.SECURITY)


class BulkDeleteRequest(BaseModel):
    recording_ids: list[uuid.UUID]


def _serialize(row: TerminalSessionRecording) -> dict:
    return {
        "id": str(row.id),
        "device_id": str(row.device_id),
        "device_hostname": row.device_hostname,
        "actor_email": row.actor_email,
        "jit_elevation_id": str(row.jit_elevation_id) if row.jit_elevation_id else None,
        "protocol": row.protocol,
        "byte_count": row.byte_count,
        "redacted": row.redacted,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "close_reason": row.close_reason,
        "in_progress": row.ended_at is None,
    }


@router.get("")
def list_recordings(
    device_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    jit_elevation_id: uuid.UUID | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _current_user: User = Depends(_reviewer_only),
):
    """Most-recent-first list, optionally filtered by device, actor, or
    the JIT elevation a session was opened under -- the three ways a
    reviewer typically starts ("what did this person do", "what
    happened on this device", "what did this specific grant get used
    for")."""
    query = db.query(TerminalSessionRecording)
    if device_id is not None:
        query = query.filter(TerminalSessionRecording.device_id == device_id)
    if actor_email is not None:
        query = query.filter(TerminalSessionRecording.actor_email == actor_email)
    if jit_elevation_id is not None:
        query = query.filter(TerminalSessionRecording.jit_elevation_id == jit_elevation_id)
    rows = query.order_by(TerminalSessionRecording.started_at.desc()).limit(min(limit, 500)).all()
    return [_serialize(r) for r in rows]


@router.get("/{recording_id}")
def get_recording(
    recording_id: uuid.UUID, db: Session = Depends(get_db), _current_user: User = Depends(_reviewer_only),
):
    row = db.get(TerminalSessionRecording, recording_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _serialize(row)


@router.get("/{recording_id}/transcript")
def get_transcript(
    recording_id: uuid.UUID, db: Session = Depends(get_db), _current_user: User = Depends(_reviewer_only),
):
    """The decoded {"t", "dir", "data"} records, for an in-app playback
    view. Use GET /{id}/download for the raw file instead."""
    row = db.get(TerminalSessionRecording, recording_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    return {"id": str(row.id), "records": session_recording_service.read_transcript(row)}


@router.get("/{recording_id}/download")
def download_recording(
    recording_id: uuid.UUID, db: Session = Depends(get_db), _current_user: User = Depends(_reviewer_only),
):
    row = db.get(TerminalSessionRecording, recording_id)
    if not row or not row.file_path:
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(
        row.file_path, media_type="application/x-ndjson",
        filename=f"terminal-session-{row.id}.jsonl",
    )


@router.delete("/{recording_id}", status_code=204)
def delete_recording(
    recording_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(_deleter_only),
):
    row = db.get(TerminalSessionRecording, recording_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Deleted Terminal Recording", result="Deleted",
        device_hostname=row.device_hostname,
        detail=f"recording_id={row.id} actor_email={row.actor_email} started_at={row.started_at}",
    )
    session_recording_service.delete_recordings(db, [row])


@router.post("/bulk-delete", status_code=200)
def bulk_delete_recordings(
    payload: BulkDeleteRequest, db: Session = Depends(get_db), current_user: User = Depends(_deleter_only),
):
    """Deletes any of the given recording IDs that exist; unknown/already-
    deleted IDs are silently skipped rather than failing the whole batch
    (matches bulk device operations elsewhere in the app)."""
    if not payload.recording_ids:
        raise HTTPException(status_code=400, detail="recording_ids must not be empty")
    rows = (
        db.query(TerminalSessionRecording)
        .filter(TerminalSessionRecording.id.in_(payload.recording_ids))
        .all()
    )
    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Bulk Deleted Terminal Recordings", result="Deleted",
        detail=f"count={len(rows)} recording_ids={[str(r.id) for r in rows]}",
    )
    deleted = session_recording_service.delete_recordings(db, rows)
    return {"deleted": deleted}


@router.delete("/", status_code=200)
def delete_all_recordings(
    device_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    jit_elevation_id: uuid.UUID | None = None,
    confirm: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(_deleter_only),
):
    """Deletes every recording matching the given filters -- the same
    three filters list_recordings supports, so "delete all" can mean
    either literally everything (no filters) or a scoped sweep ("every
    recording for this device", "everything under this actor"). Without
    at least one filter this is a full wipe of the evidence trail, so it
    additionally requires `confirm=true` -- an unfiltered call without it
    is rejected rather than silently deleting nothing or everything.
    """
    query = db.query(TerminalSessionRecording)
    if device_id is not None:
        query = query.filter(TerminalSessionRecording.device_id == device_id)
    if actor_email is not None:
        query = query.filter(TerminalSessionRecording.actor_email == actor_email)
    if jit_elevation_id is not None:
        query = query.filter(TerminalSessionRecording.jit_elevation_id == jit_elevation_id)

    unfiltered = device_id is None and actor_email is None and jit_elevation_id is None
    if unfiltered and not confirm:
        raise HTTPException(
            status_code=400,
            detail="This deletes every terminal recording with no filter applied. Pass confirm=true to proceed.",
        )

    rows = query.all()
    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Deleted All Terminal Recordings", result="Deleted",
        detail=(
            f"count={len(rows)} filters=(device_id={device_id}, actor_email={actor_email}, "
            f"jit_elevation_id={jit_elevation_id})"
        ),
    )
    deleted = session_recording_service.delete_recordings(db, rows)
    return {"deleted": deleted}
