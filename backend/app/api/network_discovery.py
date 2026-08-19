"""Network Discovery API — sweep a CIDR range for live hosts not yet in
the device inventory.

  POST   /discovery/scans                — start a scan (enqueues Celery task)
  GET    /discovery/scans                 — list past/running scans
  GET    /discovery/scans/{id}            — one scan's summary
  GET    /discovery/scans/{id}/hosts      — that scan's discovered hosts
  POST   /discovery/hosts/{id}/import     — create a Device from a discovered host
  POST   /discovery/hosts/{id}/ignore     — mark a host as reviewed/not-of-interest
  DELETE /discovery/scans/{id}            — delete a scan and its results
  GET    /discovery/schedules/{id}/ignore-rules            — list persisted ignore decisions
  DELETE /discovery/schedules/{id}/ignore-rules/{rule_id}  — revoke a persisted ignore decision

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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device, DeviceVendor
from app.models.network_discovery import (
    DiscoveredHost,
    DiscoveredHostIpamStatus,
    DiscoveryIgnoreRule,
    DiscoveryScan,
    DiscoveryScanStatus,
    DiscoverySchedule,
)
from app.models.subnet import IPAddressState, IPReservation
from app.models.user import User, UserRole
from app.schemas.network_discovery import (
    CredentialSuggestion,
    DiscoveredHostImport,
    DiscoveredHostRead,
    DiscoveredHostReserve,
    DiscoveryIgnoreRuleRead,
    DiscoveryScanCreate,
    DiscoveryScanRead,
    DiscoveryScheduleCreate,
    DiscoveryScheduleRead,
    DiscoveryScheduleUpdate,
)
from app.services import (
    credential_service,
    event_bus,
    ipam_service,
    network_discovery_service,
    reachability_service,
)

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
    """Best-effort mapping of network_discovery_service._guess_vendor's
    free-text guess onto the (deliberately small) DeviceVendor enum.

    NOTE: DeviceVendor only covers cisco/juniper/arista/linux today, but
    _guess_vendor can identify several others it has no enum slot for
    (Fortinet, Aruba, MikroTik, HP -- see its sysDescr keyword list).
    Those currently fall through to the CISCO default below, which
    silently mislabels a discovered Fortinet/Aruba/etc. device as Cisco
    on import. That's a real gap, not intentional: it doesn't break
    anything (vendor only drives which config-parsing/OS-detection path
    gets used later), but it does mean an imported non-Cisco device may
    get the wrong parser. Flagging here rather than fixing by widening
    DeviceVendor, since that enum is referenced throughout config
    parsing/backups/compliance and adding a value is a larger, separate
    change than this discovery feature.
    """
    if not guess:
        return DeviceVendor.CISCO
    return _VENDOR_ALIASES.get(guess.lower(), DeviceVendor.CISCO)


@router.get("/hosts/{host_id}/suggested-credentials", response_model=CredentialSuggestion | None)
def suggested_credentials(
    host_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Best-effort SSH/SNMP credential-profile suggestion for this host's
    guessed vendor, for the import form to pre-fill (see
    credential_service.suggest_credentials_for_vendor). Returns null when
    there isn't a confident enough match -- the import form should just
    leave the credential fields blank in that case, not treat it as an
    error.
    """
    host = db.get(DiscoveredHost, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Discovered host not found")

    # Only suggest against a vendor we're actually confident about --
    # _resolve_vendor's CISCO fallback is meant for import-time device
    # creation (every Device needs *some* vendor value), not for deciding
    # whose credentials to hand out. A host with no vendor_guess, or one
    # DeviceVendor can't represent (see _resolve_vendor's docstring),
    # should get no suggestion rather than a false-confidence Cisco one.
    if not host.vendor_guess or host.vendor_guess.lower() not in _VENDOR_ALIASES:
        return None
    return credential_service.suggest_credentials_for_vendor(db, _VENDOR_ALIASES[host.vendor_guess.lower()])


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

    from app.models.device import SnmpVersion

    device = Device(
        hostname=body.hostname,
        ip_address=host.ip_address,
        vendor=DeviceVendor(body.vendor) if body.vendor else _resolve_vendor(host.vendor_guess),
        site=body.site,
        device_type=body.device_type,
        device_role=body.device_role,
        # Credential *pointers* only (ref names, usernames, SNMP dialect)
        # -- typically pre-filled by the UI from
        # GET /discovery/hosts/{id}/suggested-credentials. No secret
        # material is ever set here; an operator still has to set the
        # actual password/community via POST /devices/{id}/ssh-credentials
        # or /snmp-credentials, same as any other newly created device.
        ssh_credential_ref=body.ssh_credential_ref,
        ssh_username=body.ssh_username,
        snmp_community_ref=body.snmp_community_ref,
        snmp_username=body.snmp_username,
        snmp_version=SnmpVersion(body.snmp_version) if body.snmp_version else None,
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
    host.imported_by = user.email
    host.imported_at = func.now()
    db.add(host)
    db.commit()
    db.refresh(host)
    return host


@router.post("/hosts/{host_id}/ignore", response_model=DiscoveredHostRead)
def ignore_host(host_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    host = db.get(DiscoveredHost, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Discovered host not found")
    host.ignored = True
    host.ignored_by = user.email
    host.ignored_at = func.now()
    db.add(host)

    # If this host came from a scheduled scan, remember the decision at
    # the schedule level too -- not just on this one host row -- so the
    # next sweep of the same range doesn't re-flag it. Fingerprinted on
    # (schedule_id, ip, vendor_guess); see DiscoveryIgnoreRule's docstring
    # for why vendor_guess is part of the key.
    scan = db.get(DiscoveryScan, host.scan_id)
    if scan and scan.schedule_id:
        rule = (
            db.query(DiscoveryIgnoreRule)
            .filter(
                DiscoveryIgnoreRule.schedule_id == scan.schedule_id,
                DiscoveryIgnoreRule.ip_address == host.ip_address,
                DiscoveryIgnoreRule.vendor_guess == host.vendor_guess,
            )
            .first()
        )
        if rule is None:
            db.add(
                DiscoveryIgnoreRule(
                    schedule_id=scan.schedule_id,
                    ip_address=host.ip_address,
                    vendor_guess=host.vendor_guess,
                    ignored_by=user.email,
                )
            )
        else:
            # Someone re-ignored (e.g. after the rule's fingerprint match
            # briefly lapsed) -- refresh who/when rather than leaving a
            # stale attribution on record.
            rule.ignored_by = user.email
            rule.ignored_at = func.now()
            db.add(rule)

    db.commit()
    db.refresh(host)
    return host


@router.post("/hosts/{host_id}/reserve", response_model=DiscoveredHostRead)
def reserve_host(
    host_id: uuid.UUID,
    body: DiscoveredHostReserve,
    db: Session = Depends(get_db),
    user: User = Depends(_discovery_admin),
):
    """Acknowledges a discovered host in place by creating an IPAM
    IPReservation for its address, so "this is fine, I know about it"
    doesn't require a separate trip to the IPAM page to hand-enter the
    same IP a second time. This is the direct complement to /import: use
    /import when the host should become a managed Device, use /reserve
    when it's a real, expected thing on the network that NetGuard
    shouldn't manage as a device (someone else's endpoint, a vendor
    appliance, etc.) but IPAM should still know is spoken for.

    Requires a managed Subnet to already cover this address -- reserving
    into IPAM only means something if IPAM is tracking that range at
    all; a host outside any configured subnet should be brought under a
    Subnet first (or just ignored here).
    """
    host = db.get(DiscoveredHost, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Discovered host not found")
    if host.imported or host.ignored:
        raise HTTPException(status_code=409, detail="This host has already been actioned")
    if host.matched_device_id:
        raise HTTPException(status_code=409, detail="This IP already matches an existing device")

    subnet = ipam_service.find_subnet_for_ip(db, host.ip_address)
    if subnet is None:
        raise HTTPException(
            status_code=422,
            detail=f"No IPAM subnet covers {host.ip_address} -- add one under IPAM before reserving.",
        )

    reservation = (
        db.query(IPReservation)
        .filter(IPReservation.subnet_id == subnet.id, IPReservation.ip_address == host.ip_address)
        .first()
    )
    if reservation is None:
        reservation = IPReservation(
            subnet_id=subnet.id,
            ip_address=host.ip_address,
            state=IPAddressState.RESERVED,
            note=body.note or f"Acknowledged from discovery scan {host.scan_id}",
        )
        db.add(reservation)
    elif reservation.state != IPAddressState.RESERVED:
        raise HTTPException(
            status_code=409,
            detail=f"{host.ip_address} is already reserved in IPAM as {reservation.state.value}, not reservable here",
        )

    host.ipam_status = DiscoveredHostIpamStatus.EXPECTED
    host.ipam_reservation_note = reservation.note
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


@router.get("/schedules/{schedule_id}/ignore-rules", response_model=list[DiscoveryIgnoreRuleRead])
def list_schedule_ignore_rules(
    schedule_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Every persisted ignore decision for this schedule -- what
    run_scan is currently auto-suppressing on each sweep. Without this,
    the effect of DiscoveryIgnoreRule is invisible: an admin has no way
    to see why a host that should be new never triggered a notification,
    or to review a growing list of suppressions for staleness (a
    decommissioned device whose old IP would otherwise stay silently
    ignored forever -- see the rule's own note field for context on why
    it was made).
    """
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return (
        db.query(DiscoveryIgnoreRule)
        .filter(DiscoveryIgnoreRule.schedule_id == schedule_id)
        .order_by(DiscoveryIgnoreRule.ignored_at.desc())
        .all()
    )


@router.delete("/schedules/{schedule_id}/ignore-rules/{rule_id}", status_code=204)
def delete_schedule_ignore_rule(
    schedule_id: uuid.UUID,
    rule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(_discovery_admin),
):
    """Revokes one persisted ignore decision -- the direct undo for a
    rule created via POST /discovery/hosts/{id}/ignore. Deleting the
    rule doesn't retroactively un-ignore any DiscoveredHost row already
    written under it (those are historical scan results, same as every
    other discovery field); it only means the *next* sweep of this
    schedule will surface that IP+vendor fingerprint again if it's still
    responsive. NETWORK_ADMIN-only, same restriction as everything else
    that changes what a scan does -- ignoring one host is a
    per-result review action anyone can do, but revoking a standing
    suppression rule is closer to reconfiguring the schedule itself.
    """
    rule = db.get(DiscoveryIgnoreRule, rule_id)
    if not rule or rule.schedule_id != schedule_id:
        raise HTTPException(status_code=404, detail="Ignore rule not found")
    db.delete(rule)
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
