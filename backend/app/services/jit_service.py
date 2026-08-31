"""Just-In-Time (JIT) role elevation: temporary, time-bound privilege
escalation, most commonly requested by a NETWORK_ENGINEER/NOC_ENGINEER/
SECURITY user to briefly hold NETWORK_ADMIN so they can push their own
approved change request without a standing admin role.

Lifecycle: pending -> (approve) -> active -> expires_at elapses -> treated
as expired. There's no Celery sweep that flips the `status` column at the
exact expiry instant -- `is_active_now` below re-checks `expires_at`
against wall-clock time on every call, which is what app.core.deps.
require_roles actually gates on. A background sweep
(mark_expired_elevations) exists purely to keep the *displayed* status
tidy for list views; it is not what enforces expiry.
"""
from __future__ import annotations

import datetime
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.change_request import ChangeRequest
from app.models.device import Device
from app.models.jit_elevation import JitElevation, JitElevationStatus
from app.models.user import User
from app.services import audit_service, notification_service


class JitDeviceNotFoundError(Exception):
    """Raised when a device-scoped JIT request names a device_id that
    doesn't exist -- caught at the API layer and turned into a 400
    rather than letting a bad UUID silently produce an unscoped grant."""

MAX_DURATION_MINUTES = 8 * 60  # 8 hours -- a JIT grant that "expires" next quarter isn't JIT

# A JIT request tied to a change request that's itself been flagged
# dangerous (Critical Risk and/or blast-radius, see
# ChangeRequest.requires_dual_approval / api.change_requests._dual_approval,
# which already reuses impact_simulation_service + topology_service.
# compute_blast_radius) gets a much shorter leash than a routine grant:
# capped duration, even if the requester asked for longer.
DANGER_MAX_DURATION_MINUTES = 60


class JitAlreadyApprovedByYouError(Exception):
    """Raised when the same admin who gave the first approval on a
    dual-approval elevation tries to give the second one too."""


def _danger_context(db: Session, change_request_id: uuid.UUID | None) -> tuple[bool, str | None]:
    """Looks up the linked change request's own risk classification and
    reports whether this JIT request should inherit its danger status.
    Reuses ChangeRequest.requires_dual_approval/dual_approval_reason
    directly rather than re-deriving risk here, so JIT and the CR always
    agree about what counts as dangerous -- one source of truth.
    """
    if change_request_id is None:
        return False, None
    cr = db.get(ChangeRequest, change_request_id)
    if cr is None or not cr.requires_dual_approval:
        return False, None
    return True, cr.dual_approval_reason


def is_active_now(elevation: JitElevation, now: datetime.datetime | None = None) -> bool:
    """True if this specific row currently grants its role -- approved
    and within its time window. Doesn't mutate `elevation.status`; callers
    that need the DB row's status column to reflect this should also call
    mark_expired_elevations.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if elevation.status != JitElevationStatus.ACTIVE:
        return False
    if elevation.expires_at is None:
        return False
    expires_at = elevation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    return expires_at > now


def is_stale(elevation: JitElevation, now: datetime.datetime | None = None) -> bool:
    """True if the DB row still says ACTIVE but its window has already
    lapsed -- i.e. mark_expired_elevations hasn't swept it yet. Doesn't
    affect enforcement (is_active_now already re-checks expires_at on
    every gate), but it's a signal worth surfacing on its own: a grant
    sitting stale for a while usually means the Celery beat sweep isn't
    running, which is worth knowing about independent of any one grant's
    correctness. Same staleness shape as topology_service's link
    `stale` flag -- a live re-check overriding a DB column that only
    gets updated lazily.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if elevation.status != JitElevationStatus.ACTIVE or elevation.expires_at is None:
        return False
    expires_at = elevation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    return expires_at <= now


def time_to_approve_seconds(elevation: JitElevation) -> float | None:
    """Seconds between request and decision for a row that's actually
    been decided on. None for still-pending rows (nothing to measure
    yet) and for rows with no requested_at (shouldn't happen -- server
    default -- but the column is nullable at the type level)."""
    if elevation.decided_at is None or elevation.requested_at is None:
        return None
    requested_at, decided_at = elevation.requested_at, elevation.decided_at
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=datetime.timezone.utc)
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=datetime.timezone.utc)
    return max((decided_at - requested_at).total_seconds(), 0.0)


def approval_metrics(db: Session, *, days: int = 30) -> dict:
    """Time-to-approve summary (mean/median/p90, in seconds) over decided
    requests in the last `days`, plus the current count of stale grants
    -- backs a small RBAC/JIT dashboard card. Rejections are excluded
    from the timing stats (an admin sitting on a request they're going
    to reject isn't the same signal as slow approval turnaround) but
    counted separately so a spike in rejections is still visible.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    decided = (
        db.query(JitElevation)
        .filter(JitElevation.decided_at.isnot(None), JitElevation.requested_at >= since)
        .all()
    )
    approved_times = sorted(
        t for row in decided if row.status != JitElevationStatus.REJECTED
        for t in [time_to_approve_seconds(row)] if t is not None
    )
    rejected_count = sum(1 for row in decided if row.status == JitElevationStatus.REJECTED)

    def _pct(sorted_vals: list[float], pct: float) -> float | None:
        if not sorted_vals:
            return None
        idx = min(int(len(sorted_vals) * pct), len(sorted_vals) - 1)
        return sorted_vals[idx]

    stale_count = sum(1 for row in db.query(JitElevation).filter(JitElevation.status == JitElevationStatus.ACTIVE).all() if is_stale(row))

    return {
        "window_days": days,
        "decided_count": len(approved_times),
        "rejected_count": rejected_count,
        "mean_seconds": (sum(approved_times) / len(approved_times)) if approved_times else None,
        "median_seconds": _pct(approved_times, 0.5),
        "p90_seconds": _pct(approved_times, 0.9),
        "stale_active_count": stale_count,
    }


def active_roles_for_user(db: Session, user_id) -> set[str]:
    """Every role currently JIT-granted to this user, right now -- what
    require_roles() unions with the user's base User.role. Small fleets /
    low grant volume, so a simple per-request query is fine (no caching).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = (
        db.query(JitElevation)
        .filter(
            JitElevation.user_id == user_id,
            JitElevation.status == JitElevationStatus.ACTIVE,
            JitElevation.expires_at.isnot(None),
            JitElevation.expires_at > now,
        )
        .all()
    )
    return {row.elevated_role for row in rows}


def current_active_elevation(db: Session, user_id) -> JitElevation | None:
    """The single most-recently-granted currently-active elevation for
    this user, if any -- used to tag a terminal session recording
    (TerminalSessionRecording.jit_elevation_id) with the grant it was
    opened under. Most-recent rather than "any" in the (rare) case a
    user somehow holds more than one active elevation at once, so the
    recording links to the grant that's actually relevant right now.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        db.query(JitElevation)
        .filter(
            JitElevation.user_id == user_id,
            JitElevation.status == JitElevationStatus.ACTIVE,
            JitElevation.expires_at.isnot(None),
            JitElevation.expires_at > now,
        )
        .order_by(JitElevation.first_approved_at.desc().nullslast())
        .first()
    )


def mark_expired_elevations(db: Session) -> int:
    """Flips any ACTIVE row whose window has lapsed to EXPIRED. Cosmetic
    housekeeping for list/history views -- see module docstring for why
    actual access enforcement never depends on this having run. Safe to
    call from a Celery beat task or lazily at the top of any JIT list
    endpoint; returns the number of rows updated.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = (
        db.query(JitElevation)
        .filter(JitElevation.status == JitElevationStatus.ACTIVE, JitElevation.expires_at <= now)
        .all()
    )
    for row in rows:
        row.status = JitElevationStatus.EXPIRED
    if rows:
        db.commit()
    return len(rows)


def _user_label(db: Session, user_id: uuid.UUID) -> str:
    """Best-effort human-readable identifier for a notification body --
    falls back to the raw id if the user row is somehow gone, since a
    notification failing to resolve a name shouldn't block the sweep."""
    user = db.get(User, user_id)
    return user.email if user is not None else str(user_id)


def sweep_expiry_notifications(db: Session, *, now: datetime.datetime | None = None) -> dict:
    """Celery beat entry point (see app.tasks.run_jit_expiry_notify_sweep_task
    / celery_app "jit-expiry-notify-sweep"). Two independent passes over
    ACTIVE elevations, each posting through the standard
    notification_service.notify() Slack/Teams/webhook/email/in-app fan-out
    so a grant lapsing mid-task doesn't go unnoticed until the holder's
    next API call gets a 403:

      1. "Expiring soon" -- any ACTIVE row within JIT_EXPIRY_WARNING_MINUTES
         of expires_at that hasn't already been warned
         (expiry_warning_sent_at is None). Fired at most once per row --
         see the column's docstring on the model.
      2. "Expired" -- delegates the actual status flip to
         mark_expired_elevations (single source of truth for that
         transition) but, unlike a bare call to it, also notifies for each
         row that just flipped, so "your access just ended" is pushed out
         rather than only ever visible on next look at the JIT Access page.

    Returns {"warned": N, "expired": N}. Safe to call on every tick even
    with nothing due -- both passes are empty-query no-ops in that case.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    warning_cutoff = now + datetime.timedelta(minutes=settings.JIT_EXPIRY_WARNING_MINUTES)

    # --- Pass 1: expiring soon -------------------------------------------------
    soon_rows = (
        db.query(JitElevation)
        .filter(
            JitElevation.status == JitElevationStatus.ACTIVE,
            JitElevation.expires_at.isnot(None),
            JitElevation.expires_at > now,
            JitElevation.expires_at <= warning_cutoff,
            JitElevation.expiry_warning_sent_at.is_(None),
        )
        .all()
    )
    for row in soon_rows:
        remaining_minutes = max(int((row.expires_at - now).total_seconds() // 60), 0)
        link = f"{settings.FRONTEND_URL.rstrip('/')}/jit-access"
        notification_service.notify(
            event="JIT Access Expiring Soon",
            message=(
                f"{_user_label(db, row.user_id)}'s temporary {row.elevated_role} access expires in "
                f"~{remaining_minutes}m ({row.expires_at.isoformat()}). Extend or wrap up before it "
                f"lapses -- <{link}|JIT Access>"
            ),
            severity="warning",
            change_request_id=row.change_request_id,
        )
        row.expiry_warning_sent_at = now
    if soon_rows:
        db.commit()

    # --- Pass 2: just expired ---------------------------------------------------
    expiring_now = (
        db.query(JitElevation)
        .filter(JitElevation.status == JitElevationStatus.ACTIVE, JitElevation.expires_at <= now)
        .all()
    )
    # Snapshot what we need before mark_expired_elevations re-queries and
    # flips status underneath us.
    expired_info = [(row.id, row.user_id, row.elevated_role, row.change_request_id) for row in expiring_now]
    expired_count = mark_expired_elevations(db)
    for _row_id, user_id, elevated_role, change_request_id in expired_info:
        notification_service.notify(
            event="JIT Access Expired",
            message=f"{_user_label(db, user_id)}'s temporary {elevated_role} access has expired.",
            severity="info",
            change_request_id=change_request_id,
        )

    return {"warned": len(soon_rows), "expired": expired_count}


def request_elevation(
    db: Session,
    *,
    user_id: uuid.UUID,
    elevated_role: str,
    reason: str,
    duration_minutes: int,
    change_request_id: uuid.UUID | None,
    requested_by_email: str,
    device_id: uuid.UUID | None = None,
    scoped_operation: str | None = None,
) -> JitElevation:
    danger, danger_reason = _danger_context(db, change_request_id)
    capped_duration = min(duration_minutes, DANGER_MAX_DURATION_MINUTES) if danger else duration_minutes

    device = None
    if device_id is not None:
        device = db.get(Device, device_id)
        if device is None:
            raise JitDeviceNotFoundError(f"device {device_id} not found")

    elevation = JitElevation(
        user_id=user_id,
        elevated_role=elevated_role,
        reason=reason,
        requested_duration_minutes=capped_duration,
        change_request_id=change_request_id,
        device_id=device_id,
        scoped_operation=scoped_operation,
        requested_by=user_id,
        status=JitElevationStatus.PENDING,
        requires_dual_approval=danger,
        dual_approval_reason=danger_reason,
    )
    db.add(elevation)
    db.commit()
    db.refresh(elevation)

    scope_note = f" scoped to device {device.hostname}" if device is not None else " (fleet-wide)"
    if device is not None and scoped_operation:
        scope_note += f", operation {scoped_operation}"
    detail = f"Requested {elevated_role} for {capped_duration}m{scope_note}: {reason}"
    if danger:
        detail += (
            f" [linked change request is {danger_reason} -- window capped at "
            f"{DANGER_MAX_DURATION_MINUTES}m"
            + (f" (requested {duration_minutes}m)" if duration_minutes != capped_duration else "")
            + ", second approver required]"
        )

    audit_service.record_event(
        db,
        actor=requested_by_email,
        action="JIT Access Requested",
        result="Pending",
        change_request_id=change_request_id,
        detail=detail,
    )
    return elevation


def approve_elevation(
    db: Session, elevation: JitElevation, *, approver_id: uuid.UUID, approver_email: str, note: str | None
) -> JitElevation:
    now = datetime.datetime.now(datetime.timezone.utc)

    # Dual approval (fed from the linked change request's risk
    # classification -- see _danger_context/request_elevation): the first
    # admin's approval is recorded but does not activate the grant. A
    # second, *different* admin has to approve again -- same shape as
    # ChangeRequest.first_approved_by/at, so the two approval flows read
    # consistently for anyone who's used either.
    if elevation.requires_dual_approval and elevation.first_approved_by is None:
        elevation.first_approved_by = approver_id
        elevation.first_approved_at = now
        db.commit()
        db.refresh(elevation)

        audit_service.record_event(
            db,
            actor=approver_email,
            action=f"JIT Access First Approval ({elevation.dual_approval_reason or 'Critical Change'})",
            result="Awaiting Second Approval",
            change_request_id=elevation.change_request_id,
            detail=(
                f"{elevation.dual_approval_reason}: a second, different Network Administrator must "
                f"approve before {elevation.elevated_role} is granted to user {elevation.user_id}."
            ),
        )
        return elevation

    if elevation.requires_dual_approval and elevation.first_approved_by == approver_id:
        raise JitAlreadyApprovedByYouError(
            f"{elevation.dual_approval_reason}: the second approval must come from a different "
            "Network Administrator."
        )

    duration = min(elevation.requested_duration_minutes, MAX_DURATION_MINUTES)
    elevation.status = JitElevationStatus.ACTIVE
    elevation.decided_by = approver_id
    elevation.decided_at = now
    elevation.decision_note = note
    elevation.activated_at = now
    elevation.expires_at = now + datetime.timedelta(minutes=duration)
    db.commit()
    db.refresh(elevation)

    action = "JIT Access Approved" if not elevation.requires_dual_approval else "JIT Access Approved (2nd of 2)"
    audit_service.record_event(
        db,
        actor=approver_email,
        action=action,
        result="Approved",
        change_request_id=elevation.change_request_id,
        detail=f"Granted {elevation.elevated_role} to user {elevation.user_id} until {elevation.expires_at.isoformat()}",
    )
    return elevation


def reject_elevation(
    db: Session, elevation: JitElevation, *, approver_id: uuid.UUID, approver_email: str, note: str | None
) -> JitElevation:
    elevation.status = JitElevationStatus.REJECTED
    elevation.decided_by = approver_id
    elevation.decided_at = datetime.datetime.now(datetime.timezone.utc)
    elevation.decision_note = note
    db.commit()
    db.refresh(elevation)

    audit_service.record_event(
        db,
        actor=approver_email,
        action="JIT Access Rejected",
        result="Rejected",
        change_request_id=elevation.change_request_id,
        detail=note or f"Denied {elevation.elevated_role} for user {elevation.user_id}",
    )
    return elevation


def revoke_elevation(
    db: Session, elevation: JitElevation, *, revoker_id: uuid.UUID, revoker_email: str, note: str | None
) -> JitElevation:
    elevation.status = JitElevationStatus.REVOKED
    elevation.revoked_by = revoker_id
    elevation.revoked_at = datetime.datetime.now(datetime.timezone.utc)
    if note:
        elevation.decision_note = note
    db.commit()
    db.refresh(elevation)

    audit_service.record_event(
        db,
        actor=revoker_email,
        action="JIT Access Revoked",
        result="Revoked",
        change_request_id=elevation.change_request_id,
        detail=note or f"Revoked {elevation.elevated_role} early for user {elevation.user_id}",
    )
    return elevation
