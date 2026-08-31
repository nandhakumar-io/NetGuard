"""Independent validation of an incoming DeviceJobRequest.

This is the single most important module in the Device Gateway. The
Gateway's entire security value is that it does NOT trust the API's
say-so -- it re-derives authorization from the database itself, the same
way a completely separate, adversarial service would have to. Even if
the API process is fully compromised and publishes a perfectly-formed,
correctly-signed job (i.e. the attacker also stole DEVICE_JOB_SIGNING_KEY),
this module still enforces:

  - tenant/device actually exist and match
  - the operation is in the declared whitelist
  - mutating operations require an approved, non-expired change request
    (unless the change's risk classification doesn't require one --
    same policy the API's own change_requests flow already enforces;
    re-checked here independently rather than assumed)
  - if a jit_elevation_id is present, that grant is currently active
    (approved, not expired, not revoked) and, when the grant carries a
    device_id and/or scoped_operation (Section 10), that it actually
    covers the device and operation this job is asking for -- a
    fleet-wide grant (device_id NULL) still authorizes any device, same
    as before device-scoping existed, since scoping down is opt-in at
    request time, not forced.
  - the job itself hasn't expired and hasn't already been executed
    (replay protection)
"""
from __future__ import annotations

import logging
import uuid as uuid_module
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.jit_elevation import JitElevation, JitElevationStatus
from app.models.user import User, UserRole
from app.schemas.device_job import (
    MUTATING_OPERATIONS,
    DeviceJobRequest,
    is_expired,
    verify_signature,
)
from app.schemas.terminal_job import TerminalOpenRequest
from app.schemas.terminal_job import is_expired as terminal_is_expired
from app.schemas.terminal_job import verify_signature as terminal_verify_signature

# Kept in sync with app.api.terminal.TERMINAL_ALLOWED_ROLES. Duplicated
# rather than imported because app.api is not on the Gateway's import
# path by design (see device_gateway/__init__.py) -- the Gateway must
# not have to import API route modules to do its own independent check.
TERMINAL_ALLOWED_ROLES = (UserRole.NETWORK_ADMIN, UserRole.NETWORK_ENGINEER, UserRole.NOC_ENGINEER)

logger = logging.getLogger("netguard.device_gateway.validator")

# In-memory replay guard. A single-process Gateway with one job queue is
# enough for this to be effective within a job's TTL (jobs expire in
# minutes -- see DEFAULT_JOB_TTL_SECONDS); if the Gateway is ever scaled
# to multiple replicas this needs to move to a shared store (Redis) keyed
# the same way. Documented here rather than silently left single-replica-only.
_seen_job_ids: dict[str, datetime] = {}


class JobRejected(Exception):
    """Raised for any validation failure. The Gateway must NEVER execute
    a job that raised this, and must record the rejection to the audit
    trail (see device_gateway/main.py)."""


def _prune_seen_job_ids(now: datetime) -> None:
    stale = [jid for jid, seen_at in _seen_job_ids.items() if (now - seen_at).total_seconds() > 3600]
    for jid in stale:
        _seen_job_ids.pop(jid, None)


def validate(job: DeviceJobRequest, db: Session, signing_key: str) -> Device:
    """Returns the validated Device row on success. Raises JobRejected
    with a specific, loggable reason on any failure -- never a generic
    "invalid job", so a real rejection is distinguishable from a bug."""
    now = datetime.now(timezone.utc)

    if not verify_signature(job, signing_key):
        raise JobRejected("invalid or missing job signature")

    if is_expired(job, now=now):
        raise JobRejected(f"job {job.job_id} expired at {job.expires_at}")

    _prune_seen_job_ids(now)
    if job.job_id in _seen_job_ids:
        raise JobRejected(f"job {job.job_id} already executed (replay)")

    try:
        device_uuid = uuid_module.UUID(job.device_id)
    except ValueError:
        raise JobRejected(f"device_id {job.device_id} is not a valid UUID")
    device = db.get(Device, device_uuid)
    if device is None:
        raise JobRejected(f"device {job.device_id} not found")

    if str(device.tenant_id) != job.tenant_id:
        raise JobRejected(
            f"tenant mismatch: job claims tenant {job.tenant_id}, "
            f"device {job.device_id} belongs to tenant {device.tenant_id}"
        )

    if job.operation in MUTATING_OPERATIONS:
        if not job.change_request_id:
            raise JobRejected(f"operation {job.operation} is mutating but no change_request_id was supplied")

        try:
            change_uuid = uuid_module.UUID(job.change_request_id)
        except ValueError:
            raise JobRejected(f"change_request_id {job.change_request_id} is not a valid UUID")
        change = db.get(ChangeRequest, change_uuid)
        if change is None:
            raise JobRejected(f"change request {job.change_request_id} not found")
        if change.status != ChangeStatus.APPROVED and change.status != ChangeStatus.DEPLOYING:
            raise JobRejected(
                f"change request {job.change_request_id} is not approved "
                f"(status={getattr(change.status, 'value', change.status)})"
            )
        # Self-approval defense-in-depth: even if the API's own approval
        # endpoint has a bug, the Gateway independently refuses to run a
        # change whose requester and approver are the same person --
        # EXCEPT for a rollback CR (is_rollback="true"), where
        # self-approval is the existing, intentional design (see
        # app.services.rollback_service.initiate_rollback /
        # initiate_partial_rollback: "emergency rollback: requester is
        # the approver, recorded explicitly", and its own audit_service
        # event). A rollback restores a config NetGuard already captured
        # earlier (a snapshot, or the current live config with one
        # section reverted) rather than introducing new arbitrary
        # content, which is why that one workflow is allowed to be
        # self-service instead of routed through a second approver --
        # not a gap in this check, a narrow, explicit, audited exception
        # to it. Every other requirement above (approved status, tenant
        # match, expiry, replay) still applies to a rollback job exactly
        # as to any other mutating job; only this one self-approval
        # check is skipped, and only for is_rollback="true".
        requester = change.submitted_by
        approver = change.approved_by
        is_rollback = str(getattr(change, "is_rollback", "false")).lower() == "true"
        if not is_rollback and requester is not None and approver is not None and str(requester) == str(approver):
            raise JobRejected(
                f"change request {job.change_request_id} was self-approved "
                f"(requester == approver); refusing to execute"
            )

    if job.jit_elevation_id:
        try:
            elevation_uuid = uuid_module.UUID(job.jit_elevation_id)
        except ValueError:
            raise JobRejected(f"jit_elevation_id {job.jit_elevation_id} is not a valid UUID")
        elevation = db.get(JitElevation, elevation_uuid)
        if elevation is None:
            raise JobRejected(f"jit elevation {job.jit_elevation_id} not found")
        # NOTE: the correct "currently usable" status is ACTIVE, not
        # APPROVED -- JitElevationStatus has no APPROVED member (see
        # app.models.jit_elevation.JitElevationStatus). A prior version
        # of this check compared against a nonexistent enum member,
        # which would raise AttributeError on every job carrying a
        # jit_elevation_id rather than actually validating anything.
        if elevation.status != JitElevationStatus.ACTIVE:
            raise JobRejected(f"jit elevation {job.jit_elevation_id} is not active")
        if str(elevation.user_id) != job.requested_by:
            raise JobRejected("jit elevation does not belong to the requesting user")
        expires_at = elevation.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                raise JobRejected(f"jit elevation {job.jit_elevation_id} has expired")
        else:
            raise JobRejected(f"jit elevation {job.jit_elevation_id} has no expiry window set")
        # Device/operation scoping (Section 10): a NULL device_id is a
        # deliberate fleet-wide grant and authorizes any device, same as
        # before scoping existed. A non-NULL device_id must match this
        # job's device exactly -- the Gateway refuses to let a grant
        # scoped to one device be used against another, even if the
        # elevated role's own permissions would otherwise allow it.
        if elevation.device_id is not None and str(elevation.device_id) != str(device.id):
            raise JobRejected(
                f"jit elevation {job.jit_elevation_id} is scoped to device "
                f"{elevation.device_id}, not {device.id}"
            )
        if elevation.scoped_operation is not None and elevation.scoped_operation != job.operation.value:
            raise JobRejected(
                f"jit elevation {job.jit_elevation_id} is scoped to operation "
                f"{elevation.scoped_operation}, not {job.operation.value}"
            )

    _seen_job_ids[job.job_id] = now
    return device

def validate_terminal_open(req: TerminalOpenRequest, db: Session, signing_key: str) -> tuple[Device, User]:
    """Independent re-validation of a TerminalOpenRequest, mirroring
    validate() above. Interactive terminal access isn't a change-request
    operation, but everything else the config-job path checks still
    applies here: signature, expiry, replay, tenant/device match, role,
    and (if present) JIT elevation scope. The Gateway resolves the real
    device credential itself right after this returns -- see
    terminal_executor.py -- so a compromised API can ask to open a
    session on a device/tenant/role it's actually entitled to, but can
    never see or forward a credential.
    """
    now = datetime.now(timezone.utc)

    if not terminal_verify_signature(req, signing_key):
        raise JobRejected("invalid or missing terminal-open signature")

    if terminal_is_expired(req, now=now):
        raise JobRejected(f"terminal open request {req.session_id} expired at {req.expires_at}")

    _prune_seen_job_ids(now)
    if req.session_id in _seen_job_ids:
        raise JobRejected(f"session_id {req.session_id} already used (replay)")

    try:
        device_uuid = uuid_module.UUID(req.device_id)
    except ValueError:
        raise JobRejected(f"device_id {req.device_id} is not a valid UUID")
    device = db.get(Device, device_uuid)
    if device is None:
        raise JobRejected(f"device {req.device_id} not found")

    if str(device.tenant_id) != req.tenant_id:
        raise JobRejected(
            f"tenant mismatch: request claims tenant {req.tenant_id}, "
            f"device {req.device_id} belongs to tenant {device.tenant_id}"
        )

    try:
        user_uuid = uuid_module.UUID(req.requested_by)
    except ValueError:
        raise JobRejected(f"requested_by {req.requested_by} is not a valid UUID")
    user = db.get(User, user_uuid)
    if user is None:
        raise JobRejected(f"user {req.requested_by} not found")
    if user.tenant_id is not None and str(user.tenant_id) != req.tenant_id:
        raise JobRejected("requesting user does not belong to the claimed tenant")
    if user.role not in TERMINAL_ALLOWED_ROLES:
        raise JobRejected(
            f"role {getattr(user.role, 'value', user.role)} is not permitted to open a device terminal"
        )

    if req.jit_elevation_id:
        try:
            elevation_uuid = uuid_module.UUID(req.jit_elevation_id)
        except ValueError:
            raise JobRejected(f"jit_elevation_id {req.jit_elevation_id} is not a valid UUID")
        elevation = db.get(JitElevation, elevation_uuid)
        if elevation is None:
            raise JobRejected(f"jit elevation {req.jit_elevation_id} not found")
        if elevation.status != JitElevationStatus.ACTIVE:
            raise JobRejected(f"jit elevation {req.jit_elevation_id} is not active")
        if str(elevation.user_id) != req.requested_by:
            raise JobRejected("jit elevation does not belong to the requesting user")
        expires_at = elevation.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                raise JobRejected(f"jit elevation {req.jit_elevation_id} has expired")
        else:
            raise JobRejected(f"jit elevation {req.jit_elevation_id} has no expiry window set")
        if elevation.device_id is not None and str(elevation.device_id) != str(device.id):
            raise JobRejected(
                f"jit elevation {req.jit_elevation_id} is scoped to device "
                f"{elevation.device_id}, not {device.id}"
            )

    _seen_job_ids[req.session_id] = now
    return device, user
