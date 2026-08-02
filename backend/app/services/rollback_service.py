"""Manual Rollback service (Change Management & Rollback).

Turns "roll this device back to snapshot X" into a normal ChangeRequest
that runs through the exact same Snapshot -> Deploy -> Health Monitor ->
Success/Rollback pipeline as any engineer-authored change (see
app.services.pipeline_service), instead of a separate ad-hoc code path.
That matters for two reasons:

  1. A rollback is still a config push to a live device, and config pushes
     can still fail -- restoring a "known good" config deserves the same
     snapshot-before-apply and real (polling) health verification that
     automatic self-healing rollback gets, not a shortcut that skips them.
  2. It's automatically visible everywhere a normal change is: the
     Deployments/Change Requests pages, the audit log, the live dashboard
     websocket -- no separate UI or tracking needed.
"""
import uuid

from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest, ChangePriority, ChangeStatus
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User
from app.services import audit_service, credential_service, deployment_engine, event_bus, snapshot_service


class RollbackError(Exception):
    """Raised when a rollback can't even be queued (bad snapshot, device busy, etc)."""


# ChangeRequest statuses that mean "a deployment for this device is already
# in flight". Rolling back while another change is mid-deploy would race
# it on the same device/SSH session, so we refuse rather than queue on top.
_IN_FLIGHT_STATUSES = (
    ChangeStatus.APPROVED,
    ChangeStatus.DEPLOYING,
    ChangeStatus.MONITORING,
)


def list_snapshots(db: Session, device_id: uuid.UUID) -> list[ConfigSnapshot]:
    """Full snapshot history for a device, newest first -- the "git log"
    a rollback UI picks a target version from (SRS 10: git-style
    configuration version control for every device). Snapshots are
    immutable, so this is a plain read.
    """
    return (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.seq.desc())
        .all()
    )


def _netmiko_type(device: Device) -> str:
    # Imported lazily to avoid a circular import (pipeline_service doesn't
    # depend on this module, but importing it at module load time here
    # would still create an import-order footgun if that ever changes).
    from app.services.pipeline_service import DEVICE_TYPE_MAP

    return DEVICE_TYPE_MAP.get(
        device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios"
    )


def initiate_rollback(
    db: Session,
    device: Device,
    snapshot: ConfigSnapshot,
    actor: User,
    reason: str | None = None,
) -> ChangeRequest:
    """Builds and auto-approves a ChangeRequest that redeploys `snapshot`'s
    config to `device`. Does not talk to the device itself -- the actual
    SSH work happens inside the standard deployment pipeline once the
    caller dispatches it (see api/devices.py), which is what gives a
    manual rollback the same safety net as every other change.

    Raises RollbackError (not an HTTP exception -- the API layer maps it)
    if the snapshot doesn't belong to the device or the device already has
    a change in flight.
    """
    if snapshot.device_id != device.id:
        raise RollbackError(f"Snapshot {snapshot.id} does not belong to device '{device.hostname}'.")

    in_flight = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.device_id == device.id, ChangeRequest.status.in_(_IN_FLIGHT_STATUSES))
        .first()
    )
    if in_flight is not None:
        raise RollbackError(
            f"Device '{device.hostname}' already has change request {in_flight.id} "
            f"in status '{in_flight.status.value}'. Wait for it to finish before rolling back."
        )

    restored_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)

    # Best-effort live read of current state, purely so the rollback's
    # diff/audit trail shows what was actually on the box right before we
    # touched it rather than just the last thing NetGuard happened to
    # deploy. A failure here is never a reason to block an emergency
    # rollback -- it just falls back to the most recent known snapshot.
    current_config = None
    try:
        ssh_password = credential_service.get_ssh_password(device)
        current_config, _used_protocol = deployment_engine.read_running_config(
            _netmiko_type(device), device.ip_address, device.ssh_username or "admin", ssh_password
        )
    except credential_service.CredentialNotFoundError:
        pass  # will fail loudly again inside the pipeline, where it's actionable

    if current_config is None:
        latest = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == device.id)
            .order_by(ConfigSnapshot.created_at.desc())
            .first()
        )
        if latest is not None:
            current_config = snapshot_service.decrypt_config(latest.running_config_encrypted)

    description = f"Rollback {device.hostname} to snapshot v{snapshot.version} ({snapshot.checksum[:12]}...)"
    if reason:
        description += f" — {reason}"

    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=actor.id,
        approved_by=actor.id,  # emergency rollback: requester is the approver, recorded explicitly
        priority=ChangePriority.EMERGENCY,
        description=description,
        business_justification=reason or "Manual rollback to a prior configuration snapshot.",
        current_config=current_config,
        proposed_config=restored_config,
        is_rollback="true",
        rollback_snapshot_id=snapshot.id,
        status=ChangeStatus.APPROVED,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    audit_service.record_event(
        db, actor=actor.email, action="Manual Rollback Requested", result="Approved",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=(
            f"target snapshot={snapshot.id} version={snapshot.version}"
            + (f" reason={reason}" if reason else "")
        ),
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

    return cr