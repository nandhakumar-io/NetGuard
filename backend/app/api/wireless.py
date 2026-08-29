"""FastAPI router for wireless AP / SSID monitoring.

Surfaces the most-recent SNMP snapshot collected by
app.services.wireless_service for the Wireless page.  Also exposes an
on-demand poll endpoint so the UI can trigger a refresh without waiting
for the next scheduled Celery run.
"""
import datetime as _datetime
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.wireless import (
    WIRELESS_AP_VENDORS,
    WirelessAPCreate,
    WirelessAPRead,
    WirelessAPUpdate,
    WirelessSSIDRead,
    WirelessSummary,
)

router = APIRouter(prefix="/wireless", tags=["wireless"])


def _ap_to_read(ap) -> "WirelessAPRead":
    return WirelessAPRead(
        id=str(ap.id),
        controller_device_id=str(ap.controller_device_id) if ap.controller_device_id else None,
        ap_index=ap.ap_index,
        ap_name=ap.ap_name,
        ap_model=ap.ap_model,
        ap_ip_address=ap.ap_ip_address,
        vendor=ap.vendor or "other",
        mac_address=ap.mac_address,
        management_ip=ap.management_ip,
        site=ap.site,
        notes=ap.notes,
        source=ap.source or "polled",
        oper_status=ap.oper_status,
        oper_status_label=ap.oper_status_label(),
        client_count=ap.client_count,
        band_2g_clients=ap.band_2g_clients,
        band_5g_clients=ap.band_5g_clients,
        created_at=ap.created_at,
        polled_at=ap.polled_at,
    )


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
    return [_ap_to_read(ap) for ap in aps]


@router.get("/aps/vendors", summary="List supported AP vendors")
def list_ap_vendors():
    return WIRELESS_AP_VENDORS


@router.post("/aps", response_model=WirelessAPRead, status_code=201, summary="Manually add an access point")
def create_ap(payload: WirelessAPCreate, db: Session = Depends(get_db)):
    """Add an AP by hand -- for standalone/unmanaged gear (TP-Link,
    Ruckus, Ubiquiti, MikroTik, etc.) that isn't behind an SNMP-polled
    WLC and therefore would never show up via poll_wireless_controller."""
    import uuid as _uuid

    from app.models.wireless import WirelessAP

    vendor = (payload.vendor or "other").lower()
    if vendor not in WIRELESS_AP_VENDORS:
        raise HTTPException(status_code=422, detail=f"vendor must be one of {WIRELESS_AP_VENDORS}")

    ap = WirelessAP(
        id=_uuid.uuid4(),
        controller_device_id=None,
        ap_index=None,
        ap_name=payload.ap_name.strip(),
        ap_model=payload.ap_model,
        ap_ip_address=payload.ap_ip_address,
        vendor=vendor,
        mac_address=payload.mac_address,
        management_ip=payload.management_ip,
        site=payload.site,
        notes=payload.notes,
        source="manual",
        client_count=payload.client_count,
    )
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return _ap_to_read(ap)


@router.patch("/aps/{ap_id}", response_model=WirelessAPRead, summary="Edit an access point")
def update_ap(ap_id: str, payload: WirelessAPUpdate, db: Session = Depends(get_db)):
    from app.models.wireless import WirelessAP

    try:
        aid = uuid.UUID(ap_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ap_id must be a valid UUID")
    ap = db.get(WirelessAP, aid)
    if ap is None:
        raise HTTPException(status_code=404, detail="Access point not found")

    data = payload.model_dump(exclude_unset=True)
    if "vendor" in data and data["vendor"]:
        vendor = data["vendor"].lower()
        if vendor not in WIRELESS_AP_VENDORS:
            raise HTTPException(status_code=422, detail=f"vendor must be one of {WIRELESS_AP_VENDORS}")
        data["vendor"] = vendor
    for field, value in data.items():
        setattr(ap, field, value)
    db.commit()
    db.refresh(ap)
    return _ap_to_read(ap)


@router.delete("/aps/{ap_id}", status_code=204, summary="Remove an access point")
def delete_ap(ap_id: str, db: Session = Depends(get_db)):
    from app.models.wireless import WirelessAP

    try:
        aid = uuid.UUID(ap_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ap_id must be a valid UUID")
    ap = db.get(WirelessAP, aid)
    if ap is None:
        raise HTTPException(status_code=404, detail="Access point not found")
    if ap.source == "polled":
        raise HTTPException(
            status_code=409,
            detail="This AP is managed by an SNMP-polled controller -- remove it from the WLC or wait for it to age out instead of deleting it here.",
        )
    db.delete(ap)
    db.commit()
    return None


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


@router.post("/aps/{ap_id}/check", summary="Check reachability of a manually-added AP")
def check_ap_reachability(ap_id: str, db: Session = Depends(get_db)):
    """For manually-added APs (no SNMP controller polling them), ping the
    management/AP IP so the UI can show a live up/down status instead of
    a permanently-empty oper_status."""
    from app.models.wireless import WirelessAP
    from app.services.reachability_service import is_reachable

    try:
        aid = uuid.UUID(ap_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ap_id must be a valid UUID")
    ap = db.get(WirelessAP, aid)
    if ap is None:
        raise HTTPException(status_code=404, detail="Access point not found")

    ip = ap.management_ip or ap.ap_ip_address
    if not ip:
        raise HTTPException(status_code=422, detail="AP has no management IP or AP IP address to check")

    reachable = is_reachable(ip)
    ap.oper_status = 1 if reachable else 0
    ap.polled_at = _datetime.datetime.now(_datetime.timezone.utc)
    db.commit()
    db.refresh(ap)
    return _ap_to_read(ap)


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
