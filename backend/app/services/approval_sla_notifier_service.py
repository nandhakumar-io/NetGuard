"""Approval SLA countdown, surfaced in Slack/Teams -- not just in-app.

GET /change-requests/pending-approvals already computes the SLA timer
(elapsed_hours / due_at / is_overdue) for the in-app queue. This module
reuses that same math to decide, per change request, whether a Slack/
Teams reminder is due, and posts it via the existing
notification_service.notify() fan-out (which already posts to
SLACK_WEBHOOK_URL / TEAMS_WEBHOOK_URL).

Two reminder stages, each posted at most once per change request (state
tracked on ChangeRequest.sla_last_notified_stage so a periodic sweep
never spams the channel every run):

  - "due_soon": the SLA window has APPROVAL_SLA_WARNING_FRACTION or less
    of its time remaining, but hasn't breached yet.
  - "overdue":  the SLA window has breached.

A change request approved/rejected/withdrawn (no longer PENDING_APPROVAL)
is simply never swept again -- no separate "cancel the reminder" state
needed since the sweep only ever looks at PENDING_APPROVAL rows.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.change_request import ChangeRequest, ChangeStatus
from app.services import notification_service

STAGE_ORDER = {None: 0, "due_soon": 1, "overdue": 2}


def _sla_state(cr: ChangeRequest, now: datetime) -> dict:
    priority_key = cr.priority.value if hasattr(cr.priority, "value") else cr.priority
    sla_hours = settings.APPROVAL_SLA_HOURS.get(priority_key, 24.0)
    created_at = cr.created_at if cr.created_at.tzinfo else cr.created_at.replace(tzinfo=timezone.utc)
    due_at = created_at + timedelta(hours=sla_hours)
    remaining_hours = (due_at - now).total_seconds() / 3600
    warning_threshold_hours = sla_hours * settings.APPROVAL_SLA_WARNING_FRACTION

    if remaining_hours <= 0:
        stage = "overdue"
    elif remaining_hours <= warning_threshold_hours:
        stage = "due_soon"
    else:
        stage = None

    return {
        "sla_hours": sla_hours,
        "due_at": due_at,
        "remaining_hours": remaining_hours,
        "stage": stage,
    }


def _format_remaining(remaining_hours: float) -> str:
    if remaining_hours <= 0:
        overdue_by = abs(remaining_hours)
        if overdue_by < 1:
            return f"overdue by {int(overdue_by * 60)}m"
        return f"overdue by {overdue_by:.1f}h"
    if remaining_hours < 1:
        return f"{int(remaining_hours * 60)}m remaining"
    return f"{remaining_hours:.1f}h remaining"


def check_and_notify(db: Session, cr: ChangeRequest, *, now: datetime | None = None) -> str | None:
    """Evaluates one PENDING_APPROVAL change request and posts a Slack/
    Teams reminder if it just crossed into a new SLA stage. Returns the
    stage posted ("due_soon" | "overdue"), or None if nothing was sent.

    Caller is responsible for db.commit() (this only mutates the ORM
    object's sla_last_notified_stage; notify() itself has no DB write).
    """
    now = now or datetime.now(timezone.utc)
    state = _sla_state(cr, now)
    stage = state["stage"]

    if stage is None:
        return None
    # Only fire when moving to a strictly later stage than the last one
    # posted -- e.g. never re-post "due_soon" after "overdue" already
    # went out, and never post either stage twice.
    if STAGE_ORDER[stage] <= STAGE_ORDER.get(cr.sla_last_notified_stage):
        return None

    priority_label = (cr.priority.value if hasattr(cr.priority, "value") else str(cr.priority)).upper()
    remaining_text = _format_remaining(state["remaining_hours"])
    link = f"{settings.FRONTEND_URL.rstrip('/')}/change-requests/{cr.id}"

    if stage == "overdue":
        severity = "critical"
        headline = f"Approval SLA breached — {priority_label} change #{str(cr.id)[:8]}"
    else:
        severity = "warning"
        headline = f"Approval SLA due soon — {priority_label} change #{str(cr.id)[:8]}"

    message = (
        f"{cr.description}\n"
        f"SLA: {remaining_text} (window: {state['sla_hours']}h) — <{link}|Review and approve>"
    )

    notification_service.notify(
        event=headline,
        message=message,
        severity=severity,
        change_request_id=cr.id,
    )

    cr.sla_last_notified_stage = stage
    return stage


def sweep_pending_approvals(db: Session) -> int:
    """Scans every PENDING_APPROVAL change request and posts any newly-
    due SLA reminders. Returns the number of reminders posted. Intended
    to run on a periodic Celery beat schedule (see
    app.tasks.run_approval_sla_notify_sweep_task); the in-app queue at
    GET /change-requests/pending-approvals computes the same timer live
    on every request and doesn't depend on this sweep.
    """
    now = datetime.now(timezone.utc)
    crs = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.status == ChangeStatus.PENDING_APPROVAL)
        .all()
    )
    notified = 0
    for cr in crs:
        if check_and_notify(db, cr, now=now):
            notified += 1
    if notified:
        db.commit()
    return notified
