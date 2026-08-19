"""Network Discovery API — sweep a CIDR range for live hosts not yet in
the device inventory.

  POST   /discovery/scans                — start a scan (enqueues Celery task)
  GET    /discovery/scans                 — list past/running scans
  GET    /discovery/scans/{id}            — one scan's summary
  GET    /discovery/scans/{id}/hosts      — that scan's discovered hosts
  POST   /discovery/hosts/{id}/import     — create a Device from a discovered host
  POST   /discovery/hosts/{id}/ignore     — mark a host as reviewed/not-of-interest
  DELETE /discovery/scans/{id}            — delete a scan and its results

A scan actively probes machines on the network (TCP connect attempts
across a range of IPs) -- same trust boundary as the reachability sweep
and SNMP polling, but operator-triggered against an operator-chosen
range rather than restricted to already-known devices, so this is
NETWORK_ADMIN-only, same restriction as app.api.webhooks and for the
same reason (an authenticated-but-untrusted caller shouldn't be able to
make the backend probe arbitrary internal ranges on demand).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device, DeviceVendor
from app.models.network_discovery import (
    DiscoveredHost,
    DiscoveryScan,
    DiscoveryScanStatus,
    DiscoverySchedule,
)
from app.models.user import User, UserRole
from app.schemas.network_discovery import (
    DiscoveredHostImport,
    DiscoveredHostRead,
    DiscoveryScanCreate,
    DiscoveryScanRead,
    DiscoveryScheduleCreate,
    DiscoveryScheduleRead,
    DiscoveryScheduleUpdate,
)
from app.services import event_bus, network_discovery_service, reachability_service

router = APIRouter(prefix="/discovery", tags=["network-discovery"])

_discovery_admin = require_roles(UserRole.NETWORK_ADMIN)


@router.post("/scans", response_model=DiscoveryScanRead, status_code=202)
def start_scan(
    body: DiscoveryScanCreate,
    db: Session = Depends(get_db),
    user: User = Depends(_discovery_admin),
):
    # Fail fast on a bad/oversized range before ever touching Celery --
    # same reasoning as _reject_unsafe_webhook_url validating at the API
    # layer rather than letting a bad row reach the worker.
    try:
        network_discovery_service.parse_and_validate_cidr(body.cidr)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    scan = DiscoveryScan(
        cidr=body.cidr,
        ports=",".join(str(p) for p in body.ports) if body.ports else None,
        snmp_community_ref=crypto.encrypt(body.snmp_community) if body.snmp_community else None,
        status=DiscoveryScanStatus.PENDING,
        started_by=user.email,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)

    from app.tasks import run_network_discovery_scan_task

    run_network_discovery_scan_task.apply_async(args=[str(scan.id), body.snmp_community])

    return scan


@router.get("/scans", response_model=list[DiscoveryScanRead])
def list_scans(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(DiscoveryScan).order_by(DiscoveryScan.started_at.desc()).limit(100).all()


@router.get("/scans/{scan_id}", response_model=DiscoveryScanRead)
def get_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    scan = db.get(DiscoveryScan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan


@router.get("/scans/{scan_id}/hosts", response_model=list[DiscoveredHostRead])
def list_scan_hosts(scan_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    scan = db.get(DiscoveryScan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return (
        db.query(DiscoveredHost)
        .filter(DiscoveredHost.scan_id == scan_id)
        .order_by(DiscoveredHost.ip_sort_key)
        .all()
    )


@router.delete("/scans/{scan_id}", status_code=204)
def delete_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(_discovery_admin)):
    scan = db.get(DiscoveryScan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    if scan.status == DiscoveryScanStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Cannot delete a scan while it's running")
    db.delete(scan)
    db.commit()


_VENDOR_ALIASES = {v.value: v for v in DeviceVendor}


def _resolve_vendor(guess: str | None) -> DeviceVendor:
    if not guess:
        return DeviceVendor.CISCO
    return _VENDOR_ALIASES.get(guess.lower(), DeviceVendor.CISCO)


@router.post("/hosts/{host_id}/import", response_model=DiscoveredHostRead)
def import_host(
    host_id: uuid.UUID,
    body: DiscoveredHostImport,
    db: Session = Depends(get_db),
    user: User = Depends(_discovery_admin),
):
    """Creates a Device row from a discovered host. Mirrors
    POST /devices's own hostname-uniqueness check and best-effort
    immediate reachability probe (app.api.devices.create_device) so a
    device imported this way looks the same in the UI as one added by
    hand -- deliberately not a call into that endpoint's function
    directly, since this needs the discovered IP/host wired in and a
    different source object (DiscoveredHost, not a fresh DeviceCreate).
    """
    host = db.get(DiscoveredHost, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Discovered host not found")
    if host.imported:
        raise HTTPException(status_code=409, detail="This host has already been imported")
    if host.matched_device_id:
        raise HTTPException(status_code=409, detail="This IP already matches an existing device")
    if db.query(Device).filter(Device.hostname == body.hostname).first():
        raise HTTPException(status_code=400, detail="Device with this hostname already exists")

    device = Device(
        hostname=body.hostname,
        ip_address=host.ip_address,
        vendor=DeviceVendor(body.vendor) if body.vendor else _resolve_vendor(host.vendor_guess),
        site=body.site,
        device_type=body.device_type,
        device_role=body.device_role,
    )
    db.add(device)
    db.commit()
    db.refresh(device)

    try:
        reachability_service.check_device(db, device)
    except Exception:  # noqa: BLE001 -- best-effort, same policy as app.api.devices.create_device
        pass

    event_bus.publish_event(
        "device_added", channel=event_bus.TOPOLOGY_CHANNEL, device_id=str(device.id), hostname=device.hostname
    )

    host.imported = True
    host.imported_device_id = device.id
    db.add(host)
    db.commit()
    db.refresh(host)
    return host


@router.post("/hosts/{host_id}/ignore", response_model=DiscoveredHostRead)
def ignore_host(host_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    host = db.get(DiscoveredHost, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Discovered host not found")
    host.ignored = True
    db.add(host)
    db.commit()
    db.refresh(host)
    return host


# --- Scheduled (recurring) discovery ---
#
# A DiscoverySchedule doesn't scan anything itself -- it's a definition
# that app.tasks.run_discovery_schedule_sweep_task (Celery beat, see
# celery_app.py) picks up on a timer and turns into a normal DiscoveryScan
# the exact same way POST /discovery/scans does, so scheduled and manual
# scans are indistinguishable in the results UI except for
# DiscoveryScanRead not surfacing schedule_id today (it's there in the
# model for the sweep task's own "was this new since last time" check).


@router.post("/schedules", response_model=DiscoveryScheduleRead, status_code=201)
def create_schedule(
    body: DiscoveryScheduleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(_discovery_admin),
):
    try:
        network_discovery_service.parse_and_validate_cidr(body.cidr)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    schedule = DiscoverySchedule(
        name=body.name,
        cidr=body.cidr,
        ports=",".join(str(p) for p in body.ports) if body.ports else None,
        snmp_community_ref=crypto.encrypt(body.snmp_community) if body.snmp_community else None,
        interval_minutes=body.interval_minutes,
        enabled=body.enabled,
        created_by=user.email,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.get("/schedules", response_model=list[DiscoveryScheduleRead])
def list_schedules(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(DiscoverySchedule).order_by(DiscoverySchedule.created_at.desc()).all()


@router.patch("/schedules/{schedule_id}", response_model=DiscoveryScheduleRead)
def update_schedule(
    schedule_id: uuid.UUID,
    body: DiscoveryScheduleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(_discovery_admin),
):
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = body.model_dump(exclude_unset=True)
    if updates.get("cidr"):
        try:
            network_discovery_service.parse_and_validate_cidr(updates["cidr"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if "ports" in updates:
        ports = updates.pop("ports")
        schedule.ports = ",".join(str(p) for p in ports) if ports else None
    if "snmp_community" in updates:
        community = updates.pop("snmp_community")
        schedule.snmp_community_ref = crypto.encrypt(community) if community else None

    for field, value in updates.items():
        setattr(schedule, field, value)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(_discovery_admin)):
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(schedule)
    db.commit()


@router.post("/schedules/{schedule_id}/run-now", response_model=DiscoveryScanRead, status_code=202)
def run_schedule_now(schedule_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(_discovery_admin)):
    """Fires one sweep for this schedule immediately, outside its normal
    interval -- doesn't reset last_run_at's clock for the next scheduled
    fire, same "manual trigger doesn't disturb the timer" behavior as
    app.api.gitops's manual-sync-vs-auto-sync-sweep split.
    """
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    community = crypto.decrypt(schedule.snmp_community_ref) if schedule.snmp_community_ref else None
    scan = DiscoveryScan(
        cidr=schedule.cidr,
        ports=schedule.ports,
        snmp_community_ref=schedule.snmp_community_ref,
        status=DiscoveryScanStatus.PENDING,
        started_by=f"schedule:{schedule.name}",
        schedule_id=schedule.id,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)

    from app.tasks import run_network_discovery_scan_task

    run_network_discovery_scan_task.apply_async(args=[str(scan.id), community])
    return scan
