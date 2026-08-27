"""Maintenance windows -- schedule a period during which alerts for a
device/site/the whole fleet are suppressed instead of paging.

  GET    /maintenance-windows            — list (optionally only active ones)
  POST   /maintenance-windows            — create/schedule a window
  GET    /maintenance-windows/{id}       — single window
  POST   /maintenance-windows/{id}/cancel — end it early
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.maintenance_window import MaintenanceWindow
from app.models.user import User, UserRole
from app.schemas.maintenance_window import (
    MaintenanceWindowCreate,
    MaintenanceWindowRead,
)
from app.services import audit_service

router = APIRouter(prefix="/maintenance-windows", tags=["maintenance-windows"])

# Same rationale as other fleet-affecting actions in this app: scheduling
# a window that silences alerting is an admin/on-call decision, not
# something every read-only viewer should be able to do.
MAINTENANCE_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN, UserRole.NETWORK_ENGINEER, UserRole.NOC_ENGINEER)


def _annotate_active(row: MaintenanceWindow) -> MaintenanceWindow:
    now = datetime.now(timezone.utc)
    starts = row.starts_at if row.starts_at.tzinfo else row.starts_at.replace(tzinfo=timezone.utc)
    ends = row.ends_at if row.ends_at.tzinfo else row.ends_at.replace(tzinfo=timezone.utc)
    row.is_active = (not row.cancelled) and starts <= now <= ends  # type: ignore[attr-defined]
    return row


@router.get("", response_model=list[MaintenanceWindowRead])
def list_windows(
    active_only: bool = Query(False, description="Only return currently-active, non-cancelled windows"),
    device_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(MaintenanceWindow)
    if device_id:
        q = q.filter(MaintenanceWindow.device_id == device_id)
    rows = q.order_by(desc(MaintenanceWindow.starts_at)).offset(offset).limit(limit).all()
    rows = [_annotate_active(r) for r in rows]
    if active_only:
        rows = [r for r in rows if r.is_active]
    return rows


@router.post("", response_model=MaintenanceWindowRead, status_code=201)
def create_window(
    payload: MaintenanceWindowCreate,
    db: Session = Depends(get_db),
    user: User = Depends(MAINTENANCE_MANAGER_ROLES),
):
    row = MaintenanceWindow(
        name=payload.name,
        reason=payload.reason,
        scope=payload.scope,
        device_id=payload.device_id,
        site=payload.site,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        created_by=user.email,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="maintenance_window_created",
        result="success",
        detail=f"{row.name} ({row.scope}) {row.starts_at.isoformat()} -> {row.ends_at.isoformat()}",
    )
    return _annotate_active(row)


@router.get("/{window_id}", response_model=MaintenanceWindowRead)
def get_window(window_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    row = db.get(MaintenanceWindow, window_id)
    if not row:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    return _annotate_active(row)


@router.post("/{window_id}/cancel", response_model=MaintenanceWindowRead)
def cancel_window(window_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(MAINTENANCE_MANAGER_ROLES)):
    row = db.get(MaintenanceWindow, window_id)
    if not row:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    if row.cancelled:
        raise HTTPException(status_code=400, detail="Window already cancelled")

    row.cancelled = True
    row.cancelled_at = datetime.now(timezone.utc)
    row.cancelled_by = user.email
    db.commit()
    db.refresh(row)

    audit_service.record_event(
        db, actor=user.email, tenant_id=user.tenant_id, action="maintenance_window_cancelled", result="success", detail=row.name
    )
    return _annotate_active(row)
