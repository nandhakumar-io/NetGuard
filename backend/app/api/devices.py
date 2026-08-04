import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.alert import Alert
from app.models.audit_log import AuditLog
from app.models.change_request import ChangeRequest
from app.models.config_drift import ConfigDrift
from app.models.deployment import Deployment, DeploymentLog, HealthCheckResult
from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.models.golden_config import GoldenConfig
from app.models.protocol_operation import ProtocolOperation
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.device import (
    DeviceCreate,
    DeviceDiscoveryResult,
    DeviceRead,
    DeviceUpdate,
    SnmpCredentialsUpdate,
    SnmpTestResult,
    SshCredentialsUpdate,
    SshTestResult,
)
from app.schemas.rollback import RollbackRequest, RollbackResponse, SnapshotSummary
from app.services import rollback_service, audit_service, metrics_service, credential_service, snmp_service, protocol_manager, reachability_service, netbox_service, eol_service
from app.services.health_monitor import ALL_CHECKS
from app.tasks import run_deployment_pipeline_task

router = APIRouter(prefix="/devices", tags=["devices"])

# Only Network Administrators manage inventory (FR-2 + RBAC); everyone authenticated can read it.
INVENTORY_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _poll_snmp_best_effort(db: Session, device: Device) -> None:
    """Fire an immediate SNMP poll synchronously (instead of queuing a
    Celery task) so the dashboard/health tab has telemetry right away even
    when no Celery worker/Redis is running -- see also the in-process
    polling loop in app.main for the recurring sweep. Best-effort: an
    unreachable device or missing credential must not block the device
    create/update response.
    """
    try:
        metrics_service.poll_device(db, device)
    except metrics_service.SnmpNotConfiguredError:
        pass
    except metrics_service.credential_service.CredentialNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - best-effort only
        pass

# Rollback carries the same authority as approving a change (both bypass
# the normal validation/approval queue), so it's gated the same way.
ROLLBACK_ROLES = require_roles(UserRole.NETWORK_ADMIN)


@router.get("", response_model=list[DeviceRead])
def list_devices(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return [DeviceRead.from_device(d) for d in db.query(Device).all()]


@router.post("", response_model=DeviceRead, status_code=201)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)):
    if db.query(Device).filter(Device.hostname == payload.hostname).first():
        raise HTTPException(status_code=400, detail="Device with this hostname already exists")
    device = Device(**payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next SNMP_POLL_INTERVAL_SECONDS
    # sweep -- if the device was added with SNMP already configured, poll
    # it right away so the dashboard / device detail page has telemetry as
    # soon as possible.
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)

    # Same idea for reachability: don't leave a freshly-added device
    # showing UNKNOWN until the next REACHABILITY_POLL_INTERVAL_SECONDS
    # sweep picks it up.
    try:
        reachability_service.check_device(db, device)
    except Exception:  # noqa: BLE001 - best-effort, same policy as the SNMP poll above
        pass

    return DeviceRead.from_device(device)


@router.get("/health-checks/catalog")
def get_health_checks_catalog(_=Depends(get_current_user)):
    """Available post-deployment verification tests (SRS 6.9)."""
    return [
        {"name": k, "description": v["label"]}
        for k, v in ALL_CHECKS.items()
    ]


@router.get("/eol-summary")
def get_eol_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide firmware/hardware EOL rollup for the dashboard badge and
    a dedicated audit view -- how many devices are past vendor End-of-
    Support (the operationally meaningful date -- no more fixes/support
    contracts) or End-of-Life, plus the actual per-device list so an
    operator doesn't have to click through the whole inventory to build
    that list by hand.
    """
    devices = db.query(Device).all()
    eos_devices, eol_devices, unknown_hostnames = [], [], []
    for device in devices:
        status = eol_service.check_device_eol(
            vendor=device.vendor.value if device.vendor else None,
            model=device.model,
            os_version=device.os_version,
        )
        if not status.matched:
            unknown_hostnames.append(device.hostname)
            continue
        entry = {
            "device_id": str(device.id),
            "hostname": device.hostname,
            "platform_label": status.platform_label,
            "eos_date": status.eos_date.isoformat() if status.eos_date else None,
            "eol_date": status.eol_date.isoformat() if status.eol_date else None,
            "days_since_eos": status.days_since_eos,
            "note": status.note,
        }
        if status.is_eol:
            eol_devices.append(entry)
        elif status.is_eos:
            eos_devices.append(entry)

    return {
        "total_devices": len(devices),
        "eos_count": len(eos_devices),
        "eol_count": len(eol_devices),
        "unknown_count": len(unknown_hostnames),
        "eos_devices": eos_devices,
        "eol_devices": eol_devices,
        "unknown_hostnames": unknown_hostnames,
    }


@router.get("/upgrade-plan")
def get_upgrade_plan(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide firmware *upgrade path* view -- distinct from
    /eol-summary above. That endpoint answers "what's already past
    vendor support"; this one answers the earlier, more useful question
    for planning purposes: "what's not on the platform's recommended
    target version yet", regardless of whether it's EOS/EOL. A device
    can be fully supported and still show up here (it's a few releases
    behind), and a device already past EOS is *also* included here if a
    target is known, since it still needs somewhere to go.

    Grouped by (vendor, platform_label, recommended_target_version) so
    an operator sees "23 Catalyst 2960s need to go to 17.9" as one line
    rather than scrolling 23 individual devices.
    """
    devices = db.query(Device).all()
    groups: dict[tuple[str, str, str], dict] = {}
    up_to_date_count = 0
    no_target_hostnames: list[str] = []

    for device in devices:
        status = eol_service.check_device_eol(
            vendor=device.vendor.value if device.vendor else None,
            model=device.model,
            os_version=device.os_version,
        )
        if not status.matched or not status.recommended_target_version:
            no_target_hostnames.append(device.hostname)
            continue
        if not status.needs_upgrade:
            up_to_date_count += 1
            continue

        key = (device.vendor.value if device.vendor else "unknown", status.platform_label, status.recommended_target_version)
        group = groups.setdefault(key, {
            "vendor": key[0],
            "platform_label": key[1],
            "recommended_target_version": key[2],
            "devices": [],
        })
        group["devices"].append({
            "device_id": str(device.id),
            "hostname": device.hostname,
            "current_os_version": device.os_version,
            "is_eos": status.is_eos,
            "is_eol": status.is_eol,
        })

    upgrade_groups = sorted(groups.values(), key=lambda g: len(g["devices"]), reverse=True)
    needs_upgrade_count = sum(len(g["devices"]) for g in upgrade_groups)

    return {
        "total_devices": len(devices),
        "needs_upgrade_count": needs_upgrade_count,
        "up_to_date_count": up_to_date_count,
        "no_target_count": len(no_target_hostnames),
        "upgrade_groups": upgrade_groups,
        "no_target_hostnames": no_target_hostnames,
    }


@router.post("/netbox-sync")
def sync_from_netbox(
    dry_run: bool = Query(False, description="Preview the sync without writing any changes"),
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Pull-syncs the device inventory from NetBox (see
    app.services.netbox_service) -- devices are matched/updated by
    netbox_id (falling back to hostname for a device that predates this
    sync), created if new, and never deleted here (a device removed from
    NetBox stays in NetGuard until someone explicitly removes it, since
    it may still have deployment/drift/audit history worth keeping).
    """
    try:
        result = netbox_service.sync_devices(db, dry_run=dry_run)
    except netbox_service.NetBoxSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not dry_run and (result["created"] or result["updated"]):
        audit_service.record_event(
            db, actor=current_user.email, action="NetBox Sync", result="Success",
            detail=(
                f"Created {len(result['created'])}, updated {len(result['updated'])}, "
                f"skipped {len(result['skipped'])} (of {result['netbox_devices_seen']} seen)."
            ),
        )
    return result


@router.get("/{device_id}", response_model=DeviceRead)
def get_device(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return DeviceRead.from_device(device)


@router.patch("/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: uuid.UUID, payload: DeviceUpdate, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)
):
    """Partial update -- e.g. enabling SNMP monitoring on a device that was
    added before its community string / SNMPv3 credentials were set up.
    Only fields present in the request body are changed.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    updates = payload.model_dump(exclude_unset=True)
    if "hostname" in updates and updates["hostname"] != device.hostname:
        if db.query(Device).filter(Device.hostname == updates["hostname"]).first():
            raise HTTPException(status_code=400, detail="Device with this hostname already exists")

    was_snmp_enabled = device.supports_snmp

    for field, value in updates.items():
        if field == "enabled_health_checks":
            # Device.enabled_health_checks is a Text column storing a
            # JSON-encoded list (see DeviceRead.from_device / schemas/device.py).
            # `value` here is the deserialized list[str] | None from
            # DeviceUpdate -- it must be re-serialized before it's written,
            # otherwise a raw list gets shoved into the Text column and the
            # very next read (`json.loads` on a non-string) silently fails,
            # falls back to None, and the operator's selection appears to
            # have vanished/reverted to "run everything" the moment they
            # navigate away and back.
            import json as _json

            setattr(device, field, _json.dumps(value) if value else None)
        else:
            setattr(device, field, value)

    try:
        db.commit()
        db.refresh(device)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to update device: {exc}")

    if device.supports_snmp and not was_snmp_enabled:
        _poll_snmp_best_effort(db, device)

    return DeviceRead.from_device(device)


@router.delete("/{device_id}", status_code=204)
def delete_device(
    device_id: uuid.UUID,
    force: bool = Query(False, description="Also permanently delete this device's change/deployment/config history"),
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Delete a device and ALL its related records.

    If the device has compliance-relevant history (change requests,
    deployments, config snapshots, golden configs) the caller must pass
    ``?force=true`` — otherwise a 409 is raised so the UI can warn the
    operator. Pure telemetry (alerts, drift, metrics, protocol ops) is
    always purged since it has no standalone value once the device is gone.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    blocking_counts = {
        "change_requests": db.query(func.count(ChangeRequest.id)).filter(ChangeRequest.device_id == device_id).scalar(),
        "deployments": db.query(func.count(Deployment.id)).filter(Deployment.device_id == device_id).scalar(),
        "config_snapshots": db.query(func.count(ConfigSnapshot.id)).filter(ConfigSnapshot.device_id == device_id).scalar(),
        "golden_config": db.query(func.count(GoldenConfig.id)).filter(GoldenConfig.device_id == device_id).scalar(),
    }
    blocking_counts = {k: v for k, v in blocking_counts.items() if v}

    if blocking_counts and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' has change/deployment history and cannot be deleted without confirmation. "
                    "Retry with ?force=true to permanently delete the device along with this history."
                ),
                "counts": blocking_counts,
            },
        )

    try:
        # ------- Purge ALL child rows before deleting the device -------
        # Pure telemetry: no compliance value — always purged.
        db.query(Alert).filter(Alert.device_id == device_id).delete(synchronize_session=False)
        db.query(ConfigDrift).filter(ConfigDrift.device_id == device_id).delete(synchronize_session=False)
        db.query(DeviceMetric).filter(DeviceMetric.device_id == device_id).delete(synchronize_session=False)
        db.query(ProtocolOperation).filter(ProtocolOperation.device_id == device_id).delete(synchronize_session=False)

        # Compliance-relevant records (only reached when force=true or counts are 0).
        # Deletion order: children before parents.
        # deployment_logs / health_check_results -> deployments -> change_requests
        deployment_ids = [
            d.id for d in db.query(Deployment.id).filter(Deployment.device_id == device_id).all()
        ]
        if deployment_ids:
            db.query(DeploymentLog).filter(DeploymentLog.deployment_id.in_(deployment_ids)).delete(synchronize_session=False)
            db.query(HealthCheckResult).filter(HealthCheckResult.deployment_id.in_(deployment_ids)).delete(synchronize_session=False)
        db.query(Deployment).filter(Deployment.device_id == device_id).delete(synchronize_session=False)

        change_request_ids = [
            cr.id for cr in db.query(ChangeRequest.id).filter(ChangeRequest.device_id == device_id).all()
        ]
        if change_request_ids:
            # Audit history is immutable — detach the FK rather than deleting.
            db.query(AuditLog).filter(AuditLog.change_request_id.in_(change_request_ids)).update(
                {"change_request_id": None}, synchronize_session=False
            )
            # ChangeRequest.rollback_snapshot_id -> config_snapshots.id and
            # ConfigSnapshot.change_request_id -> change_requests.id form a
            # circular FK (see the note in alembic/versions/0001_baseline.py).
            # Any change request for this device that went through a
            # rollback has rollback_snapshot_id pointing at one of the
            # ConfigSnapshot rows deleted right below -- detach it first or
            # that delete throws an IntegrityError that (unlike the one
            # guarded further down) used to propagate unhandled, producing
            # a raw 500 that skips CORSMiddleware entirely and shows up in
            # the browser as an opaque "Network Error" instead of a real
            # message.
            db.query(ChangeRequest).filter(ChangeRequest.id.in_(change_request_ids)).update(
                {"rollback_snapshot_id": None}, synchronize_session=False
            )
        db.query(ConfigSnapshot).filter(ConfigSnapshot.device_id == device_id).delete(synchronize_session=False)
        db.query(GoldenConfig).filter(GoldenConfig.device_id == device_id).delete(synchronize_session=False)
        db.query(ChangeRequest).filter(ChangeRequest.device_id == device_id).delete(synchronize_session=False)

        if blocking_counts:
            audit_service.record_event(
                db, actor=current_user.email, action="Device Force-Deleted", result="Deleted",
                device_hostname=device.hostname,
                detail=f"Purged history: {blocking_counts}",
            )

        db.delete(device)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' still has related records that prevent deletion. "
                    "Retry with ?force=true to permanently delete the device along with all its history."
                ),
                "counts": {"related_records": 1},
            },
        )
    except SQLAlchemyError:
        # Belt-and-suspenders alongside the IntegrityError branch above:
        # any *other* unexpected DB error partway through this multi-table
        # purge (a future FK this function doesn't know about yet, a
        # constraint added later, etc.) should still roll back and surface
        # as a real, catchable 409 -- not propagate as a raw exception.
        # (Even if it did propagate, app.main's global exception handler
        # now converts it to a proper CORS-safe 500 instead of the
        # unreadable "Network Error" this used to produce -- but failing
        # cleanly here gives a much more actionable message than that.)
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' could not be deleted due to an unexpected database error. "
                    "Retry with ?force=true, or check the server logs for the underlying cause."
                ),
                "counts": {"related_records": 1},
            },
        )


@router.get("/{device_id}/snapshots", response_model=list[SnapshotSummary])
def list_device_snapshots(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Config version history for a device (SRS 10: git-style config
    version control), newest first. Pick a `version`'s `id` to pass to
    POST /devices/{device_id}/rollback.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return rollback_service.list_snapshots(db, device_id)


@router.post("/{device_id}/rollback", response_model=RollbackResponse, status_code=202)
def rollback_device(
    device_id: uuid.UUID,
    payload: RollbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Manually roll a device back to a prior configuration snapshot.

    Builds and auto-approves a change request that redeploys the chosen
    snapshot, then queues it on the same deployment pipeline used for
    ordinary approved changes (Snapshot -> Deploy -> Health Monitor ->
    Success / Automatic Rollback) -- so the restore itself is snapshotted
    first and its health is verified just like any other deployment.

    Returns immediately (202) with the change_request_id; poll
    GET /change-requests/{id} or GET /deployments?change_request_id={id}
    for progress, same as approving a normal change request.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, payload.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        cr = rollback_service.initiate_rollback(db, device, snapshot, current_user, reason=payload.reason)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    run_deployment_pipeline_task.delay(str(cr.id), current_user.email)

    return RollbackResponse(
        change_request_id=cr.id,
        status=cr.status.value,
        message=f"Rollback queued for {device.hostname}. Track progress via the change request or deployments feed.",
    )


@router.post("/{device_id}/clear-unstable-flag", response_model=DeviceRead)
def clear_unstable_flag(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Manual review sign-off for the deployment pipeline circuit breaker:
    clears `flagged_unstable` so automated deploys against this device are
    allowed again. Only Network Administrators may clear it (same RBAC as
    inventory management / rollback) -- this is a deliberate "I've looked
    at what's wrong with this device" action, never automatic.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.flagged_unstable:
        raise HTTPException(status_code=400, detail="Device is not currently flagged unstable")

    device.flagged_unstable = False
    device.unstable_since = None
    db.commit()
    db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="Unstable Flag Cleared", result="Success",
        device_hostname=device.hostname,
        detail="Manual review completed; automated deploys re-enabled.",
    )
    return DeviceRead.from_device(device)


@router.post("/{device_id}/snmp-credentials", response_model=DeviceRead)
def set_snmp_credentials(
    device_id: uuid.UUID,
    payload: SnmpCredentialsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Sets/updates SNMP secrets for a device: the v1/v2c community string,
    or the SNMPv3 auth/privacy passphrases. Encrypted at rest (Fernet, see
    app.core.crypto) and never returned by any GET endpoint -- DeviceRead
    only exposes a derived `snmp_credentials_configured` boolean. Only
    fields present in the request are touched; omit a field to leave it
    unchanged, or send "" to explicitly clear it.

    This does not itself verify the credentials work -- use
    POST /devices/{id}/snmp-credentials/test for that, either before or
    after saving.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    credential_service.set_snmp_credentials(
        device,
        community=payload.community,
        v3_auth_key=payload.v3_auth_key,
        v3_priv_key=payload.v3_priv_key,
    )
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next scheduled sweep to see
    # whether the credentials they just entered actually work -- poll
    # immediately (best-effort; failures here don't block the response,
    # same as the create/enable-SNMP paths above).
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)
        db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="SNMP Credentials Updated", result="Success",
        device_hostname=device.hostname,
        detail="community" if payload.community is not None else "v3 auth/priv keys",
    )

    return DeviceRead.from_device(device)


@router.post("/{device_id}/metrics/poll")
def poll_device_metrics(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """On-demand SNMP poll for a device's telemetry."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.supports_snmp:
        raise HTTPException(status_code=400, detail="SNMP monitoring is not enabled for this device")

    try:
        metrics_service.poll_device(db, device)
    except metrics_service.credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"On-demand poll failed: {exc}")
    
    return {"status": "success"}


@router.post("/{device_id}/snmp-credentials/test", response_model=SnmpTestResult)
def test_snmp_credentials(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Verifies SNMP connectivity using whatever credentials are currently
    on file for this device (DB-encrypted first, legacy env-var ref as
    fallback -- see credential_service), without doing a full health poll.
    Lets an operator confirm a community string / SNMPv3 credential set
    actually works right after saving it, instead of waiting for the next
    scheduled poll to find out it doesn't.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.snmp_version:
        raise HTTPException(status_code=400, detail="Device has no SNMP version configured (snmp_version is unset)")

    try:
        auth = metrics_service.build_snmp_auth(device)
    except credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from app.core.config import settings

    result = snmp_service.test_connection(device.ip_address, auth, timeout=settings.SNMP_TIMEOUT_SECONDS)
    return SnmpTestResult(**result)


@router.get("/{device_id}/discovery", response_model=DeviceDiscoveryResult)
def discover_device(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """On-demand SNMP discovery: hostname, ARP table, routing table, LLDP/
    CDP neighbors, and chassis/module inventory (snmp_service.discover_inventory).
    Heavier than a routine health poll (several full table walks), so this
    runs only when requested, not on the scheduled polling cadence.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.supports_snmp or not device.snmp_version:
        raise HTTPException(status_code=400, detail="Device has no SNMP configured (supports_snmp/snmp_version unset)")

    try:
        auth = metrics_service.build_snmp_auth(device)
    except credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from app.core.config import settings

    result = snmp_service.discover_inventory(device.ip_address, auth, timeout=settings.SNMP_TIMEOUT_SECONDS)
    return DeviceDiscoveryResult(
        device_id=device.id,
        hostname=device.hostname,
        reported_hostname=result["hostname"],
        arp_table=result["arp_table"],
        routing_table=result["routing_table"],
        lldp_neighbors=result["lldp_neighbors"],
        cdp_neighbors=result["cdp_neighbors"],
        inventory=result["inventory"],
        retrieved_at=datetime.datetime.utcnow(),
    )


@router.post("/{device_id}/ssh-credentials", response_model=DeviceRead)
def set_ssh_credentials(
    device_id: uuid.UUID,
    payload: SshCredentialsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Sets/updates the SSH login for a device: username (plain) and
    password (Fernet-encrypted at rest, see app.core.crypto). Never
    returned by any GET endpoint -- DeviceRead only exposes a derived
    `ssh_credentials_configured` boolean.

    This is the actual credential entry point for NETCONF/RESTCONF/SSH
    deployments, config backups, and topology link inference (all of
    which need a real running-config read to succeed) -- ssh_credential_ref
    alone is only a *pointer* to a NETGUARD_CRED_<REF> env var and was
    never something an operator could set from the UI.

    Only fields present in the request are touched; omit a field to leave
    it unchanged, or send password="" to explicitly clear it. This does
    not itself verify the credential works -- use
    POST /devices/{id}/ssh-credentials/test for that, either before or
    after saving.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if payload.username is not None:
        device.ssh_username = payload.username
    if payload.password is not None:
        credential_service.set_ssh_password(device, payload.password)

    db.commit()
    db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="SSH Credentials Updated", result="Success",
        device_hostname=device.hostname,
        detail="username" if payload.username is not None else "password",
    )

    return DeviceRead.from_device(device)


@router.post("/{device_id}/ssh-credentials/test", response_model=SshTestResult)
def test_ssh_credentials(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verifies SSH/NETCONF/RESTCONF connectivity using whatever
    credentials are currently on file for this device (DB-encrypted
    password first, legacy env-var ref as fallback -- see
    credential_service), by reusing the exact same read that backups,
    drift detection, and topology link inference depend on
    (ProtocolManager.get_running_config) rather than opening a separate
    test-only connection. Lets an operator confirm a password actually
    works right after saving it, instead of waiting for the next
    scheduled backup/poll to find out it doesn't.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    used_protocol = protocol_manager.select_protocol(device)
    result = protocol_manager.ProtocolManager(db, device, operator=current_user.email).get_running_config()
    return SshTestResult(
        success=result.success,
        message=result.error or f"Connected via {used_protocol} and read the running config.",
        protocol=used_protocol if result.success else None,
    )


@router.get("/{device_id}/protocol-operations")
def list_device_protocol_operations(
    device_id: uuid.UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Recent NETCONF/RESTCONF/SNMP operations recorded against this device
    (config reads/pushes, health checks, SNMP polls) -- backs the Protocol
    Operations tab on the device detail page. Complements the coarser
    AuditLog with the raw request/response payloads captured by
    ProtocolManager for every operation it performs.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    ops = (
        db.query(ProtocolOperation)
        .filter(ProtocolOperation.device_id == device_id)
        .order_by(ProtocolOperation.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(op.id),
            "protocol": op.protocol.value if hasattr(op.protocol, "value") else str(op.protocol),
            "operation": op.operation,
            "operator": op.operator,
            "success": op.success,
            "error_message": op.error_message,
            "http_status": op.http_status,
            "execution_time_ms": op.execution_time_ms,
            "created_at": op.created_at,
        }
        for op in ops
    ]