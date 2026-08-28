"""FastAPI router for wireless AP / SSID monitoring.

Surfaces the most-recent SNMP snapshot collected by
app.services.wireless_service for the Wireless page.  Also exposes an
on-demand poll endpoint so the UI can trigger a refresh without waiting
for the next scheduled Celery run.
"""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.wireless import WirelessAPRead, WirelessSSIDRead, WirelessSummary

router = APIRouter(prefix="/wireless", tags=["wireless"])


def _resolve_controller(db: Session, controller_id: str):
    """Return the Device for *controller_id*, 404 if not found."""
    from app.models.device import Device
    try:
        cid = uuid.UUID(controller_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="controller_id must be a valid UUID")
    device = db.get(Device, cid)
    if device is None:
        raise HTTPException(status_code=404, detail="Controller device not found")
    return device


# ---------------------------------------------------------------------------
# List controllers (devices that have any wireless snapshot)
# ---------------------------------------------------------------------------

@router.get("/controllers", summary="List devices with wireless data")
def list_controllers(db: Session = Depends(get_db)):
    """Returns the distinct set of controller device IDs / hostnames that
    have at least one WirelessAP row -- useful for populating the UI dropdown."""
    from sqlalchemy import distinct

    from app.models.device import Device
    from app.models.wireless import WirelessAP

    rows = (
        db.query(distinct(WirelessAP.controller_device_id))
        .all()
    )
    controllers = []
    for (cid,) in rows:
        dev = db.get(Device, cid)
        controllers.append({
            "device_id": str(cid),
            "hostname": dev.hostname if dev else None,
            "ip_address": dev.ip_address if dev else None,
        })
    return controllers


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@router.get("/summary/{controller_id}", response_model=WirelessSummary)
def get_summary(controller_id: str, db: Session = Depends(get_db)):
    from app.services.wireless_service import get_wireless_summary
    device = _resolve_controller(db, controller_id)
    summary = get_wireless_summary(db, device.id, controller_hostname=device.hostname)
    return summary


# ---------------------------------------------------------------------------
# AP list
# ---------------------------------------------------------------------------

@router.get("/aps", response_model=list[WirelessAPRead])
def list_aps(controller_id: str | None = None, db: Session = Depends(get_db)):
    """List access points.  Optionally filtered to a single controller."""
    from app.models.wireless import WirelessAP
    q = db.query(WirelessAP)
    if controller_id:
        try:
            cid = uuid.UUID(controller_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="controller_id must be a valid UUID")
        q = q.filter(WirelessAP.controller_device_id == cid)
    aps = q.order_by(WirelessAP.ap_name).all()

    result = []
    for ap in aps:
        d = {
            "id": str(ap.id),
            "controller_device_id": str(ap.controller_device_id),
            "ap_index": ap.ap_index,
            "ap_name": ap.ap_name,
            "ap_model": ap.ap_model,
            "ap_ip_address": ap.ap_ip_address,
            "oper_status": ap.oper_status,
            "oper_status_label": ap.oper_status_label(),
            "client_count": ap.client_count,
            "band_2g_clients": ap.band_2g_clients,
            "band_5g_clients": ap.band_5g_clients,
            "polled_at": ap.polled_at,
        }
        result.append(WirelessAPRead(**d))
    return result


# ---------------------------------------------------------------------------
# SSID list
# ---------------------------------------------------------------------------

@router.get("/ssids", response_model=list[WirelessSSIDRead])
def list_ssids(controller_id: str | None = None, db: Session = Depends(get_db)):
    """List SSID profiles.  Optionally filtered to a single controller."""
    from app.models.wireless import WirelessSSID
    q = db.query(WirelessSSID)
    if controller_id:
        try:
            cid = uuid.UUID(controller_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="controller_id must be a valid UUID")
        q = q.filter(WirelessSSID.controller_device_id == cid)
    ssids = q.order_by(WirelessSSID.ssid_name).all()

    result = []
    for s in ssids:
        d = {
            "id": str(s.id),
            "controller_device_id": str(s.controller_device_id),
            "ssid_index": s.ssid_index,
            "ssid_name": s.ssid_name,
            "admin_status": s.admin_status,
            "enabled": s.admin_status == 1,
            "mobile_station_count": s.mobile_station_count,
            "polled_at": s.polled_at,
        }
        result.append(WirelessSSIDRead(**d))
    return result


# ---------------------------------------------------------------------------
# On-demand poll trigger
# ---------------------------------------------------------------------------

def _do_poll(controller_id: str) -> None:
    """Background task: open a fresh DB session and call poll_wireless_controller."""
    import uuid as _uuid

    from app.core.database import SessionLocal
    from app.models.device import Device
    from app.services.wireless_service import poll_wireless_controller
    db = SessionLocal()
    try:
        device = db.get(Device, _uuid.UUID(controller_id))
        if device:
            poll_wireless_controller(db, device)
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()


@router.post("/poll/{controller_id}", summary="Trigger on-demand wireless poll")
def trigger_poll(controller_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Kicks off an immediate SNMP poll for the given controller in a
    background task.  Returns immediately; the client should re-fetch
    /wireless/aps and /wireless/ssids a few seconds later."""
    device = _resolve_controller(db, controller_id)
    background_tasks.add_task(_do_poll, str(device.id))
    return {"status": "polling", "controller_id": str(device.id), "hostname": device.hostname}
