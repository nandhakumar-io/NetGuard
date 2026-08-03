import datetime
import json
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
from app.models.discovered_neighbor import DiscoveredNeighbor
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
from app.services import rollback_service, audit_service, metrics_service, credential_service, snmp_service, protocol_manager
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
    data = payload.model_dump()
    checks = data.pop("enabled_health_checks", None)
    device = Device(**data, enabled_health_checks=json.dumps(checks) if checks else None)
    db.add(device)
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next SNMP_POLL_INTERVAL_SECONDS
    # sweep -- if the device was added with SNMP already configured, poll
    # it right away so the dashboard / device detail page has telemetry as
    # soon as possible.
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)

    return DeviceRead.from_device(device)


@router.get("/health-checks/catalog")
def list_health_check_catalog(_=Depends(get_current_user)):
    """The full set of post-deployment verification checks (health_monitor
    .ALL_CHECKS) a device's `enabled_health_checks` can select from, so the
    UI can render a picker instead of hardcoding check names.
    """
    from app.services import health_monitor

    return [
        {"name": name, "category": meta["category"], "label": meta["label"]}
        for name, meta in health_monitor.ALL_CHECKS.items()
    ]


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

    if "enabled_health_checks" in updates:
        checks = updates.pop("enabled_health_checks")
        device.enabled_health_checks = json.dumps(checks) if checks else None

    for field, value in updates.items():
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

    try:
        blocking_counts = {
            "change_requests": db.query(func.count(ChangeRequest.id)).filter(ChangeRequest.device_id == device_id).scalar(),
            "deployments": db.query(func.count(Deployment.id)).filter(Deployment.device_id == device_id).scalar(),
            "config_snapshots": db.query(func.count(ConfigSnapshot.id)).filter(ConfigSnapshot.device_id == device_id).scalar(),
            "golden_config": db.query(func.count(GoldenConfig.id)).filter(GoldenConfig.device_id == device_id).scalar(),
        }
        blocking_counts = {k: v for k, v in blocking_counts.items() if v}
    except SQLAlchemyError:
        # Same "never let this fall through unhandled" reasoning as the
        # purge below -- an error here used to propagate raw, skip
        # CORSMiddleware, and show up in the browser as a bare "Network
        # Error" with no status/body at all.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Could not check '{device.hostname}' for related history due to a database error. Please try again.",
                "counts": {},
            },
        )

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
        # DiscoveredNeighbor has two FKs into devices (device_id, the
        # discovering device, and neighbor_device_id, a resolved
        # neighbor) -- both need clearing or this device can never be
        # deleted even with force=true (every retry hits the same
        # IntegrityError since nothing ever purges these rows).
        db.query(DiscoveredNeighbor).filter(DiscoveredNeighbor.device_id == device_id).delete(synchronize_session=False)
        db.query(DiscoveredNeighbor).filter(DiscoveredNeighbor.neighbor_device_id == device_id).update(
            {"neighbor_device_id": None}, synchronize_session=False
        )

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

    _persist_discovered_neighbors(db, device, result)

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


def _resolve_neighbor_device_id(db: Session, name: str | None) -> uuid.UUID | None:
    """Best-effort match of a raw LLDP/CDP-reported neighbor identity
    (usually a hostname, sometimes an IP) against the known device
    inventory, so the Topology graph can draw a real edge instead of
    just displaying the raw string. Matches on exact hostname (the
    common case) or IP address; anything else is left unresolved rather
    than guessed at (e.g. no fuzzy/partial hostname matching), since a
    wrong topology edge is worse than a missing one.
    """
    if not name:
        return None
    candidate = name.split(".")[0]  # LLDP/CDP sysNames are sometimes FQDNs; devices are stored by short hostname
    device = (
        db.query(Device)
        .filter((Device.hostname == name) | (Device.hostname == candidate) | (Device.ip_address == name))
        .first()
    )
    return device.id if device else None


def _persist_discovered_neighbors(db: Session, device: Device, result: dict) -> None:
    """Replaces device's prior DiscoveredNeighbor rows with the fresh
    LLDP/CDP results from this run. Best-effort: a persistence failure
    here should never fail the discovery response itself (the operator
    still gets to see the live discovery data even if the DB write has
    a problem), so this is not wrapped in the same transaction/response
    path as anything else on this endpoint.
    """
    try:
        db.query(DiscoveredNeighbor).filter(DiscoveredNeighbor.device_id == device.id).delete()

        for n in result.get("lldp_neighbors", []):
            db.add(
                DiscoveredNeighbor(
                    device_id=device.id,
                    protocol="lldp",
                    local_port=n.get("local_port_index"),
                    neighbor_name=n.get("neighbor_name"),
                    neighbor_port=n.get("neighbor_port"),
                    neighbor_device_id=_resolve_neighbor_device_id(db, n.get("neighbor_name")),
                )
            )
        for n in result.get("cdp_neighbors", []):
            db.add(
                DiscoveredNeighbor(
                    device_id=device.id,
                    protocol="cdp",
                    local_port=n.get("local_if_index"),
                    neighbor_name=n.get("neighbor_id"),
                    neighbor_port=n.get("neighbor_port"),
                    neighbor_platform=n.get("neighbor_platform"),
                    neighbor_device_id=_resolve_neighbor_device_id(db, n.get("neighbor_id")),
                )
            )
        db.commit()
    except SQLAlchemyError:
        db.rollback()


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