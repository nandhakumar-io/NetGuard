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

from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User
from app.services import (
    audit_service,
    config_section_service,
    credential_service,
    deployment_engine,
    diff_engine,
    event_bus,
    snapshot_service,
)


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


def preview_rollback(db: Session, device: Device, snapshot: ConfigSnapshot) -> dict:
    """Read-only counterpart to initiate_rollback: builds the exact same
    "what's live right now vs. what this snapshot would restore" diff,
    but creates no ChangeRequest and pushes nothing to the device. Used
    by the rollback confirmation modal so the diff is shown *before* the
    user confirms, not only afterward in the change request / audit log.

    Raises RollbackError if the snapshot doesn't belong to the device --
    same validation as initiate_rollback, so a preview can never be
    requested for a mismatched snapshot/device pair either.
    """
    if snapshot.device_id != device.id:
        raise RollbackError(f"Snapshot {snapshot.id} does not belong to device '{device.hostname}'.")

    target_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)

    # Best-effort live read, same fallback chain as initiate_rollback:
    # live config -> most recent snapshot -> nothing to compare against.
    # A failed live read is never fatal to a *preview* (there's nothing
    # to roll back yet), it just downgrades what "current" means in the
    # response so the caller can show that honestly instead of a diff
    # against stale data with no indication it's stale.
    current_config = None
    current_source = "unavailable"
    try:
        ssh_password = credential_service.get_ssh_password(device)
        current_config, _used_protocol = deployment_engine.read_running_config(
            _netmiko_type(device), device.ip_address, device.ssh_username or "admin", ssh_password
        )
        current_source = "live"
    except credential_service.CredentialNotFoundError:
        pass
    except Exception:
        # Any live-read failure (unreachable device, auth failure, etc.)
        # falls back the same way initiate_rollback does -- a preview
        # should degrade gracefully, not 500 just because the device is
        # down right now.
        pass

    if current_config is None:
        latest = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == device.id)
            .order_by(ConfigSnapshot.created_at.desc())
            .first()
        )
        if latest is not None:
            current_config = snapshot_service.decrypt_config(latest.running_config_encrypted)
            current_source = "last_snapshot"

    warning = None
    if current_source == "last_snapshot":
        warning = (
            "Could not read the device's live configuration -- this preview compares the rollback "
            "target against the most recent snapshot on file instead, which may not reflect what's "
            "actually running right now."
        )
    elif current_source == "unavailable":
        warning = (
            "No live configuration could be read and no prior snapshot exists to compare against -- "
            "showing the rollback target only."
        )

    diff_text = diff_engine.generate_diff(current_config, target_config)
    added, removed = 0, 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    in_flight = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.device_id == device.id, ChangeRequest.status.in_(_IN_FLIGHT_STATUSES))
        .first()
    )

    return {
        "device_id": device.id,
        "snapshot_id": snapshot.id,
        "target_version": snapshot.version,
        "current_source": current_source,
        "diff": diff_text,
        "identical": current_config == target_config,
        "added_lines": added,
        "removed_lines": removed,
        "warning": warning,
        "blocked": in_flight is not None,
        "blocked_reason": (
            f"Device '{device.hostname}' already has change request {in_flight.id} in status "
            f"'{in_flight.status.value}'. It must finish before this rollback can be confirmed."
            if in_flight is not None
            else None
        ),
    }


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


# ---------------------------------------------------------------------------
# Partial / section-level rollback
#
# A full rollback restores the entire snapshot. That's the right call when
# a device is broken in ways that are hard to isolate, but for the common
# case -- "the ACL I just pushed broke something, put just that ACL back" --
# restoring the whole config is more blast radius than the incident calls
# for: it also reverts anything else that changed on the box since that
# snapshot, intentionally or not. These functions build a proposed config
# that is the device's *current* config with only one section swapped back
# to the snapshot's version, then run it through the exact same
# Snapshot -> Deploy -> Health Monitor pipeline as any other change.
# ---------------------------------------------------------------------------


def _current_config_for_device(db: Session, device: Device) -> tuple[str | None, str]:
    """Best-effort live read with the same live -> last-snapshot ->
    unavailable fallback chain used elsewhere in this module."""
    try:
        ssh_password = credential_service.get_ssh_password(device)
        current_config, _used_protocol = deployment_engine.read_running_config(
            _netmiko_type(device), device.ip_address, device.ssh_username or "admin", ssh_password
        )
        return current_config, "live"
    except credential_service.CredentialNotFoundError:
        pass
    except Exception:
        pass

    latest = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device.id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )
    if latest is not None:
        return snapshot_service.decrypt_config(latest.running_config_encrypted), "last_snapshot"
    return None, "unavailable"


def list_rollback_sections(db: Session, device: Device) -> list[dict]:
    """Sections available to partially roll back, derived from the
    device's current (live, or last-snapshot-as-fallback) configuration.
    Powers the "what do you want to revert?" picker in the rollback
    confirmation modal, alongside the existing full-snapshot list."""
    current_config, current_source = _current_config_for_device(db, device)
    if not current_config:
        return []
    return [
        {"key": s.key, "kind": s.kind, "name": s.name, "line_count": len(s.lines)}
        for s in config_section_service.list_sections(current_config)
    ] if current_source else []


def preview_partial_rollback(db: Session, device: Device, snapshot: ConfigSnapshot, section_key: str) -> dict:
    """Read-only preview of a section-level rollback: shows the diff
    that reverting only `section_key` (e.g. "ACL:BLOCK_TELNET") to its
    version in `snapshot` would produce, without touching the device or
    creating a ChangeRequest."""
    if snapshot.device_id != device.id:
        raise RollbackError(f"Snapshot {snapshot.id} does not belong to device '{device.hostname}'.")

    target_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
    current_config, current_source = _current_config_for_device(db, device)

    if not current_config:
        raise RollbackError(
            f"No live or previously-snapshotted configuration is available for '{device.hostname}' "
            "to compute a partial rollback against."
        )

    try:
        new_config, section_info = config_section_service.build_partial_config(
            current_config, target_config, section_key
        )
    except ValueError as exc:
        raise RollbackError(str(exc)) from exc

    diff_text = diff_engine.generate_diff(current_config, new_config)

    in_flight = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.device_id == device.id, ChangeRequest.status.in_(_IN_FLIGHT_STATUSES))
        .first()
    )

    return {
        "device_id": device.id,
        "snapshot_id": snapshot.id,
        "section_key": section_key,
        "section": section_info,
        "current_source": current_source,
        "diff": diff_text,
        "identical": current_config == new_config,
        "blocked": in_flight is not None,
        "blocked_reason": (
            f"Device '{device.hostname}' already has change request {in_flight.id} in status "
            f"'{in_flight.status.value}'. It must finish before this rollback can be confirmed."
            if in_flight is not None
            else None
        ),
    }


def initiate_partial_rollback(
    db: Session,
    device: Device,
    snapshot: ConfigSnapshot,
    section_key: str,
    actor: User,
    reason: str | None = None,
) -> ChangeRequest:
    """Builds and auto-approves a ChangeRequest that reverts only
    `section_key` to its version in `snapshot`, leaving the rest of the
    device's current config untouched. Runs through the same
    Snapshot -> Deploy -> Health Monitor -> Rollback pipeline as any
    other change -- including automatic full-config rollback if *this*
    (smaller) change still fails its health checks."""
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

    target_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
    current_config, _current_source = _current_config_for_device(db, device)
    if not current_config:
        raise RollbackError(
            f"No live or previously-snapshotted configuration is available for '{device.hostname}' "
            "to compute a partial rollback against."
        )

    try:
        new_config, section_info = config_section_service.build_partial_config(
            current_config, target_config, section_key
        )
    except ValueError as exc:
        raise RollbackError(str(exc)) from exc

    description = (
        f"Partial rollback of {device.hostname}: {section_info['kind']} '{section_info['name']}' "
        f"-> snapshot v{snapshot.version} ({snapshot.checksum[:12]}...)"
    )
    if reason:
        description += f" — {reason}"

    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=actor.id,
        approved_by=actor.id,
        priority=ChangePriority.EMERGENCY,
        description=description,
        business_justification=reason or "Section-level rollback to a prior configuration snapshot.",
        current_config=current_config,
        proposed_config=new_config,
        is_rollback="true",
        rollback_snapshot_id=snapshot.id,
        status=ChangeStatus.APPROVED,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    audit_service.record_event(
        db, actor=actor.email, action="Partial Rollback Requested", result="Approved",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=(
            f"section={section_key} target snapshot={snapshot.id} version={snapshot.version}"
            + (f" reason={reason}" if reason else "")
        ),
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

    return cr
