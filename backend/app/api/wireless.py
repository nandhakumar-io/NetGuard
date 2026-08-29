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
    UnregisteredApRead,
    WirelessAPCreate,
    WirelessAPRead,
    WirelessAPUpdate,
    WirelessSSIDRead,
    WirelessSummary,
)

router = APIRouter(prefix="/wireless", tags=["wireless"])


def _ap_to_read(ap, correlation: dict | None = None, device_match: dict | None = None) -> "WirelessAPRead":
    correlation = correlation or {}
    device_match = device_match or {}
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
        ap_up_time=ap.ap_up_time,
        ap_software_version=ap.ap_software_version,
        ap_serial_number=ap.ap_serial_number,
        channel_2g=ap.channel_2g,
        channel_5g=ap.channel_5g,
        tx_power_2g=ap.tx_power_2g,
        tx_power_5g=ap.tx_power_5g,
        noise_2g=ap.noise_2g,
        noise_5g=ap.noise_5g,
        channel_util_2g=ap.channel_util_2g,
        channel_util_5g=ap.channel_util_5g,
        created_at=ap.created_at,
        polled_at=ap.polled_at,
        switch_device_id=correlation.get("device_id"),
        switch_hostname=correlation.get("hostname"),
        switch_port=correlation.get("port"),
        matched_device_id=device_match.get("device_id"),
        matched_device_hostname=device_match.get("hostname"),
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
    from app.services import wireless_service
    q = db.query(WirelessAP)
    if controller_id:
        try:
            cid = uuid.UUID(controller_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="controller_id must be a valid UUID")
        q = q.filter(WirelessAP.controller_device_id == cid)
    aps = q.order_by(WirelessAP.ap_name).all()
    # Bulk correlation (one query each, not one per AP card) for
    # switchport location and "already a managed Device" config-backup
    # linking -- see wireless_service for what each of these actually does.
    switchports = wireless_service.find_switchports_for_aps(db, aps)
    device_matches = wireless_service.find_matching_device_for_ap(db, aps)
    return [
        _ap_to_read(ap, correlation=switchports.get(str(ap.id)), device_match=device_matches.get(str(ap.id)))
        for ap in aps
    ]


@router.get(
    "/unregistered-aps",
    response_model=list[UnregisteredApRead],
    summary="Switchports with an AP-like LLDP neighbor not tracked on this page",
)
def list_unregistered_aps(db: Session = Depends(get_db)):
    """See wireless_service.find_unregistered_aps -- the vendor-agnostic,
    LLDP-based substitute for Cisco AireOS's rogue-AP MIB, useful for a
    fleet without a WLC to poll rogue-AP traps from at all (e.g.
    standalone Ruckus/TP-Link Omada APs)."""
    from app.services import wireless_service
    return wireless_service.find_unregistered_aps(db)


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


@router.post(
    "/aps/from-discovery/{host_id}",
    response_model=WirelessAPRead,
    status_code=201,
    summary="Import a Discovery-scan host as a wireless AP",
)
def create_ap_from_discovery(host_id: str, db: Session = Depends(get_db)):
    """For a DiscoveredHost that's a standalone AP (Ruckus/TP-Link Omada/
    Ubiquiti/etc with no WLC to poll) -- pre-fills vendor and a best-effort
    model from the sysDescr/OUI guess Discovery already made, same as the
    existing /discovery/hosts/{id}/import general-Device path does for
    switches and routers. See wireless_service.import_ap_from_discovered_host."""
    from app.models.network_discovery import DiscoveredHost
    from app.services import wireless_service

    try:
        hid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="host_id must be a valid UUID")
    host = db.get(DiscoveredHost, hid)
    if host is None:
        raise HTTPException(status_code=404, detail="Discovered host not found")
    try:
        ap = wireless_service.import_ap_from_discovered_host(db, host)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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


@router.get(
    "/sticky-clients",
    summary="Clients dwelling on a congested AP instead of roaming",
)
def list_sticky_clients(
    controller_id: str,
    min_dwell_minutes: int = 30,
    util_threshold_pct: int = 70,
    db: Session = Depends(get_db),
):
    """See wireless_service.get_sticky_clients for what "sticky" means
    here and its limits (dwell + AP-load proxy, not true multi-AP RSSI
    stickiness detection)."""
    from app.services import wireless_service
    device = _resolve_controller(db, controller_id)
    return wireless_service.get_sticky_clients(
        db, device.id, min_dwell_minutes=min_dwell_minutes, util_threshold_pct=util_threshold_pct
    )


@router.get(
    "/co-channel-report",
    summary="APs on the same switch sharing a radio channel (self-inflicted interference)",
)
def get_co_channel_report(controller_id: str | None = None, db: Session = Depends(get_db)):
    """See wireless_service.get_co_channel_report -- "same switch" is
    used as the physical-adjacency proxy since NetGuard has no RF
    survey/geolocation data for APs. Omit controller_id to report across
    every managed AP, since co-channel interference isn't scoped to a
    single WLC."""
    from app.services import wireless_service
    cid = None
    if controller_id:
        cid = _resolve_controller(db, controller_id).id
    return wireless_service.get_co_channel_report(db, cid)


@router.get(
    "/aps/{ap_id}/history",
    summary="Historical client/utilization/noise trend for one AP",
)
def get_ap_history(ap_id: str, hours: int = 24, limit: int = 500, db: Session = Depends(get_db)):
    """Backed by VictoriaMetrics (wireless_service.poll_wireless_controller
    pushes every poll's AP gauges there) -- wireless_aps itself only ever
    holds the latest snapshot, so this is the only source for "was this
    AP degraded at 2pm yesterday"."""
    import datetime as _dt

    from app.core import vm_client

    try:
        aid = uuid.UUID(ap_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ap_id must be a valid UUID")
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(hours=hours)
    step_seconds = max(60, int((hours * 3600) / max(limit, 1)))
    rows = vm_client.ap_metric_history(aid, start, end, step_seconds)
    return rows[-limit:]


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
