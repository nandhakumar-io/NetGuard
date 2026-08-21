"""Escalation Policies: "unacknowledged for N minutes -> notify secondary
contact" (NOC on-call escalation).

Distinct from AlertRule (which decides *whether* to raise an alert) and
from alert_correlation_service (which decides whether an alert is a
*consequence* of another) -- this is purely about response time on an
alert that's already active: nobody has acknowledged it inside the
policy's window, so widen the notification blast radius instead of
waiting quietly.

run_escalation_sweep (app.tasks.run_escalation_sweep_task, every
ESCALATION_SWEEP_INTERVAL_SECONDS) is the only caller. For every enabled
policy, find active alerts (unresolved, unacknowledged, not
topology/maintenance/snooze-suppressed -- escalating noise that's already
flagged as not-urgent would defeat the point) matching the policy's
severity scope that have been open longer than unack_minutes and either
haven't escalated yet, or have (repeat_minutes set) and it's been at
least repeat_minutes since the last escalation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSeverity
from app.models.escalation_policy import EscalationPolicy
from app.models.on_call_schedule import OnCallSchedule
from app.services import (
    audit_service,
    notification_service,
    on_call_service,
    push_service,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 3.0


def _matches_scope(alert: Alert, scope: str) -> bool:
    if scope == "all":
        return True
    return alert.severity == AlertSeverity(scope)


def _due(alert: Alert, policy: EscalationPolicy, now: datetime) -> bool:
    age_minutes = (now - alert.created_at).total_seconds() / 60.0
    if not alert.escalated:
        return age_minutes >= policy.unack_minutes
    if not policy.repeat_minutes:
        return False
    since_last = (now - (alert.last_escalated_at or alert.escalated_at)).total_seconds() / 60.0
    return since_last >= policy.repeat_minutes


def _resolve_contacts(db: Session, policy: EscalationPolicy) -> str | None:
    """Who to name as "escalated to" in the message body -- the current
    on-call rotation contact if the policy has a schedule attached,
    otherwise the policy's static secondary_contacts list unchanged.
    Doesn't affect *delivery* (notify()/push still fan out the same way
    regardless), just who gets named -- there's no per-user email/push
    routing here, only a fleet-wide notify() and phone pushes any
    subscribed device receives.
    """
    if policy.on_call_schedule_id:
        schedule = db.get(OnCallSchedule, policy.on_call_schedule_id)
        if schedule:
            contact, _ = on_call_service.current_contact(schedule)
            if contact:
                return contact
    return policy.secondary_contacts


def _send(db: Session, policy: EscalationPolicy, alert: Alert) -> None:
    message = (
        f"Alert unacknowledged for {policy.unack_minutes}+ minutes: "
        f"[{alert.severity.value.upper()}] {alert.category} — {alert.message}"
    )
    contacts = _resolve_contacts(db, policy)
    if policy.channel.value == "email":
        # Reuses the standard notify() fan-out (Slack/Teams/email/in-app)
        # but callers still get a targeted heads-up: the resolved
        # contact(s) are appended to the message body since notify()
        # itself only supports the fleet-wide NOTIFY_EMAIL_RECIPIENTS
        # list, not a per-policy recipient override.
        if contacts:
            message = f"{message}\n\nEscalated to: {contacts}"
        notification_service.notify(
            event="Alert Escalated",
            message=message,
            severity=alert.severity.value,
            device_hostname=None,
        )
    elif policy.channel.value == "push":
        # PUSH: the policy's own dedicated channel, for teams that want
        # "escalate straight to someone's phone" without also wiring up a
        # webhook/Slack/Teams URL. This is the same delivery path as the
        # unconditional push below, so it isn't repeated a second time
        # for this policy (see the guard on that call).
        push_service.send_push(
            db,
            title=f"🚨 Alert Escalated ({policy.name})",
            message=message,
            severity=alert.severity.value,
        )
        # Still write an in-app Notification Center row so the escalation
        # is visible even if nobody's phone is subscribed to push.
        notification_service.notify(event="Alert Escalated", message=message, severity=alert.severity.value)
    else:
        # WEBHOOK / SLACK / TEAMS: post directly to the policy's own
        # webhook, independent of the fleet-wide SLACK_WEBHOOK_URL /
        # TEAMS_WEBHOOK_URL settings, so a secondary on-call channel can
        # differ from the primary noise channel.
        if not policy.webhook_url:
            logger.warning("Escalation policy %s has channel=%s but no webhook_url set", policy.id, policy.channel.value)
        else:
            try:
                httpx.post(policy.webhook_url, json={"text": f"🚨 NetGuard Escalation — {message}"}, timeout=TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Escalation webhook post failed for policy %s", policy.id, exc_info=True)
        # Still write an in-app Notification Center row + fleet default
        # channels so the escalation is visible even if the webhook post
        # above fails or nobody's watching that channel.
        notification_service.notify(event="Alert Escalated", message=message, severity=alert.severity.value)

    # Mobile push, on top of whichever channel above -- an escalation is
    # by definition something that already sat unacknowledged past its
    # window, so it's exactly the "nobody's staring at the dashboard"
    # case a phone push is for. Runs regardless of policy.channel (except
    # PUSH itself, already sent above -- this would otherwise double-fire
    # the exact same push) so a team using SLACK/TEAMS/EMAIL for the
    # primary escalation channel still gets a push as the secondary,
    # wake-someone-up path.
    # send_push itself only reaches devices subscribed to this severity
    # (critical-only by default), so warning-scope policies won't buzz a
    # phone unless that device opted into non-critical pushes.
    if policy.channel.value != "push":
        push_service.send_push(
            db,
            title=f"🚨 Alert Escalated ({policy.name})",
            message=message,
            severity=alert.severity.value,
        )


def run_escalation_sweep(db: Session) -> int:
    """Evaluate every enabled EscalationPolicy against active alerts.
    Returns the number of (alert, policy) escalations fired this sweep.
    """
    now = datetime.now(timezone.utc)
    policies = db.query(EscalationPolicy).filter(EscalationPolicy.enabled == True).all()
    if not policies:
        return 0

    candidates = (
        db.query(Alert)
        .filter(
            Alert.resolved == False,
            Alert.acknowledged == False,
            Alert.suppressed == False,
            Alert.suppressed_by_window_id.is_(None),
        )
        .all()
    )
    if not candidates:
        return 0

    fired = 0
    for policy in policies:
        for alert in candidates:
            if not _matches_scope(alert, policy.severity_scope.value):
                continue
            if not _due(alert, policy, now):
                continue

            _send(db, policy, alert)

            alert.escalated = True
            alert.escalated_at = alert.escalated_at or now
            alert.last_escalated_at = now
            alert.escalation_count = (alert.escalation_count or 0) + 1
            alert.escalation_policy_id = policy.id
            db.add(alert)

            audit_service.record_event(
                db,
                actor="system:escalation",
                action="Alert Escalated",
                result=policy.name,
                detail=f"alert_id={alert.id} policy_id={policy.id} unack_minutes={policy.unack_minutes}",
            )
            fired += 1

    if fired:
        db.commit()
    return fired


def list_escalated_alerts(db: Session, limit: int = 100) -> list[Alert]:
    return (
        db.query(Alert)
        .filter(Alert.escalated == True)
        .order_by(Alert.last_escalated_at.desc().nullslast(), Alert.escalated_at.desc())
        .limit(limit)
        .all()
    )
