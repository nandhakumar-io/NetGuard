"""Hop-by-hop Path/Route Tracing API (NetPath-style)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.device import Device
from app.models.path_trace import PathTrace
from app.models.user import User
from app.schemas.path_trace import PathTraceRead, PathTraceRequest
from app.services import path_trace_service

router = APIRouter(prefix="/path-trace", tags=["path-trace"])


def _to_read(db: Session, trace: PathTrace) -> PathTraceRead:
    source = db.get(Device, trace.source_device_id) if trace.source_device_id else None
    target = db.get(Device, trace.target_device_id) if trace.target_device_id else None
    payload = PathTraceRead.model_validate(trace)
    payload.source_hostname = source.hostname if source else None
    payload.target_hostname = target.hostname if target else None
    return payload


@router.post("", response_model=PathTraceRead)
def create_path_trace(
    body: PathTraceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Runs a new trace from `source_device_id` to either
    `target_device_id` (a managed device) or `target_input` (a typed
    hostname/IP), and persists the full hop-by-hop result."""
    target_input = body.target_input
    if body.target_device_id is not None:
        target_device = db.get(Device, body.target_device_id)
        if target_device is None:
            raise HTTPException(status_code=404, detail="Target device not found")
        target_input = target_input or target_device.hostname
    if not target_input:
        raise HTTPException(status_code=422, detail="Provide either target_device_id or target_input")

    trace = path_trace_service.run_trace(
        db,
        source_device_id=body.source_device_id,
        target_input=target_input,
        target_device_id=body.target_device_id,
        requested_by=getattr(current_user, "email", None),
    )
    return _to_read(db, trace)


@router.get("", response_model=list[PathTraceRead])
def list_path_traces(
    device_id: uuid.UUID | None = Query(None, description="Filter to traces with this device as source or target"),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Trace history, newest first -- for the Path Trace page's sidebar
    of recent runs."""
    query = db.query(PathTrace)
    if device_id:
        query = query.filter(
            (PathTrace.source_device_id == device_id) | (PathTrace.target_device_id == device_id)
        )
    traces = query.order_by(PathTrace.created_at.desc()).limit(limit).all()
    return [_to_read(db, t) for t in traces]


@router.get("/{trace_id}", response_model=PathTraceRead)
def get_path_trace(trace_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    trace = db.get(PathTrace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Path trace not found")
    return _to_read(db, trace)
