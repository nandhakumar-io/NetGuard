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
from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.models.user import User, UserRole
from app.schemas.metrics import (
    DeviceHealthSummary,
    DeviceMetricRead,
    FleetAvailabilitySummary,
    FleetHealthSummary,
    UnstableDevice,
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
def get_fleet_health_summary(
    vendor: str | None = Query(None, description="Scope the rollup to one vendor, e.g. 'juniper'"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Top-of-dashboard rollup: how many SNMP-monitored devices are
    currently green/yellow/red. Optionally scoped to a single vendor
    (e.g. ?vendor=juniper) so the UI's vendor fleet tabs can reuse this
    endpoint instead of a separate one."""
    return metrics_service.fleet_health_summary(db, vendor=vendor)


@router.get("/metrics/fleet-availability", response_model=FleetAvailabilitySummary)
def get_fleet_availability(
    hours: int = Query(24, ge=1, le=8760, description="Rollup window in hours"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Classic NOC "how many nines" fleet uptime number, time-weighted
    from DeviceStatusHistory over the requested window (default 24h).
    Also returns the 10 worst-availability devices in the window so the
    dashboard can show what's dragging the number down."""
    return metrics_service.fleet_availability_summary(db, hours=hours)


@router.get("/metrics/unstable-devices", response_model=list[UnstableDevice])
def get_unstable_devices(
    hours: int = Query(24, ge=1, le=8760, description="Rollup window in hours"),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """"Top flapping devices" widget: combines reachability flaps +
    interface flaps + config drift events into one ranked instability
    list (see app.services.metrics_service.unstable_devices)."""
    return metrics_service.unstable_devices(db, hours=hours, limit=limit)


@router.get("/metrics/health", response_model=list[DeviceHealthSummary])
def list_all_device_health(
    vendor: str | None = Query(None, description="Only include devices of this vendor, e.g. 'juniper'"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """One card per SNMP-enabled device for the Health Dashboard grid.
    ?vendor=juniper (etc.) narrows the grid to that vendor's fleet --
    handy for spot-checking a vendor that's had OID/parsing issues
    without hunting for its devices in the full mixed-vendor grid."""
    query = db.query(Device).filter(Device.supports_snmp.is_(True))
    if vendor:
        from app.models.device import DeviceVendor

        try:
            query = query.filter(Device.vendor == DeviceVendor(vendor.lower()))
        except ValueError:
            return []
    return [metrics_service.device_health(db, d) for d in query.all()]


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
