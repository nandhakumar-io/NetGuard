"""SNMP Monitoring / Health Dashboard API.

Read endpoints (latest reading, history, health summaries, alerts feed) are
open to any authenticated user, same as the rest of the dashboard/read
surface. Triggering an on-demand poll is gated the same way manual
rollback/inventory-management is, since it reaches out and talks to the
device over the network.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.alert import Alert, AlertSeverity
from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.models.user import User, UserRole
from app.schemas.metrics import (
    AlertRead,
    DeviceHealthSummary,
    DeviceMetricRead,
    FleetHealthSummary,
)
from app.services import metrics_service

router = APIRouter(tags=["metrics"])

POLL_NOW_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _get_device(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


# --- Fleet-wide (Health Dashboard) ---


@router.get("/metrics/health-summary", response_model=FleetHealthSummary)
def get_fleet_health_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Top-of-dashboard rollup: how many SNMP-monitored devices are
    currently green/yellow/red."""
    return metrics_service.fleet_health_summary(db)


@router.get("/metrics/health", response_model=list[DeviceHealthSummary])
def list_all_device_health(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """One card per SNMP-enabled device for the Health Dashboard grid."""
    devices = db.query(Device).filter(Device.supports_snmp.is_(True)).all()
    return [metrics_service.device_health(db, d) for d in devices]


@router.get("/alerts", response_model=list[AlertRead])
def list_alerts(
    device_id: uuid.UUID | None = None,
    severity: str | None = None,
    acknowledged: bool | None = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Alert Engine feed powering the Health Dashboard's alert panel --
    threshold breaches from SNMP polls plus any inbound SNMP traps,
    newest first."""
    query = db.query(Alert)
    if device_id is not None:
        query = query.filter(Alert.device_id == device_id)
    if severity is not None:
        query = query.filter(Alert.severity == AlertSeverity(severity))
    if acknowledged is not None:
        query = query.filter(Alert.acknowledged == acknowledged)
    return query.order_by(Alert.created_at.desc()).limit(limit).all()


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertRead)
def acknowledge_alert(alert_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.acknowledged = True
    db.commit()
    db.refresh(alert)
    return alert


# --- Per-device ---


@router.get("/devices/{device_id}/health", response_model=DeviceHealthSummary)
def get_device_health(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Latest health snapshot for one device (Health Dashboard device
    detail view)."""
    device = _get_device(db, device_id)
    return metrics_service.device_health(db, device)


@router.get("/devices/{device_id}/metrics/latest", response_model=DeviceMetricRead)
def get_latest_metric(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    latest = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device.id)
        .order_by(DeviceMetric.polled_at.desc())
        .first()
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="No metrics recorded yet for this device")
    return latest


@router.get("/devices/{device_id}/metrics/history", response_model=list[DeviceMetricRead])
def get_metric_history(
    device_id: uuid.UUID,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Historical Charts: CPU/memory/temperature/interface-utilization/
    errors over time for one device, oldest-first."""
    device = _get_device(db, device_id)
    return metrics_service.metric_history(db, device.id, hours=hours, limit=limit)


@router.post("/devices/{device_id}/metrics/poll", response_model=DeviceMetricRead)
def poll_device_now(device_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(POLL_NOW_ROLES)):
    """On-demand SNMP poll (outside the regular Celery interval) -- e.g. a
    "Refresh now" button on the device's health detail view."""
    device = _get_device(db, device_id)
    try:
        return metrics_service.poll_device(db, device)
    except metrics_service.SnmpNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except metrics_service.credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))