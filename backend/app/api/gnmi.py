"""gNMI streaming-telemetry status + credential setup. See
app.services.gnmi_service for the actual SUBSCRIBE sessions this reports
on -- this API is read-mostly (device connection settings are edited via
the normal PATCH /devices/{id}, same as supports_netconf/supports_restconf).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.device import Device
from app.models.user import User
from app.services import credential_service

router = APIRouter(prefix="/gnmi", tags=["gnmi"])


class GnmiDeviceStatus(BaseModel):
    device_id: uuid.UUID
    hostname: str
    supports_gnmi: bool
    subscribed: bool  # a live SUBSCRIBE task is currently running for this device
    last_gnmi_update_at: str | None = None
    last_gnmi_error: str | None = None


class GnmiCredentials(BaseModel):
    username: str
    password: str


@router.get("/status", response_model=list[GnmiDeviceStatus])
def gnmi_status(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """One row per gNMI-enabled device: whether a subscription task is
    actually running right now (not just whether the device is flagged
    supports_gnmi) plus the last update/error timestamps -- lets an
    operator tell "streaming, just quiet since nothing changed" apart
    from "never actually connected".

    Reads Device.gnmi_subscription_active/heartbeat_at, written by the
    supervisor's reconcile loop in the device-gateway process (see
    app.services.gnmi_service.GnmiSupervisor._write_heartbeat) -- NOT
    gnmi_service.get_supervisor(), which would always return None here
    since the supervisor moved out of this (`api`) process as part of
    the Section 4 device-credential key re-scoping. A heartbeat older
    than 2x the reconcile interval is treated as stale (Gateway down or
    unreachable) rather than trusting a possibly-ancient active flag.
    """
    from datetime import datetime, timedelta, timezone

    stale_after = timedelta(seconds=2 * settings.GNMI_DEVICE_ROSTER_REFRESH_SECONDS)
    now = datetime.now(timezone.utc)

    devices = db.query(Device).filter(Device.supports_gnmi.is_(True)).all()
    return [
        GnmiDeviceStatus(
            device_id=d.id,
            hostname=d.hostname,
            supports_gnmi=d.supports_gnmi,
            subscribed=bool(
                d.gnmi_subscription_active
                and d.gnmi_subscription_heartbeat_at is not None
                and (now - d.gnmi_subscription_heartbeat_at) <= stale_after
            ),
            last_gnmi_update_at=d.last_gnmi_update_at.isoformat() if d.last_gnmi_update_at else None,
            last_gnmi_error=d.last_gnmi_error,
        )
        for d in devices
    ]


@router.post("/devices/{device_id}/credentials")
def set_gnmi_credentials(
    device_id: uuid.UUID,
    payload: GnmiCredentials,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.gnmi_username = payload.username
    credential_service.set_gnmi_password(device, payload.password)
    db.commit()
    return {"status": "ok"}
