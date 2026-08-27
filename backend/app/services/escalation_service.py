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
from app.models.device import Device
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


def _policies_for_tenant(
    all_policies: list[EscalationPolicy], tenant_id
) -> list[EscalationPolicy]:
    """Resolve the effective policy set for a device's tenant: every
    global (tenant_id IS NULL) policy plus this tenant's own, with a
    tenant policy that names a parent_policy_id replacing (not
    supplementing) the global policy it overrides -- same override
    semantics as app.models.alert_rule_engine.evaluate_rules. A tenant
    policy with no parent_policy_id is additive.
    """
    candidates = [p for p in all_policies if p.tenant_id is None or p.tenant_id == tenant_id]
    overridden_ids = {
        p.parent_policy_id
        for p in candidates
        if p.tenant_id is not None and p.parent_policy_id is not None
    }
    return [p for p in candidates if p.enabled and p.id not in overridden_ids]


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


def _send(db: Session, policy: EscalationPolicy, alert: Alert, tenant_id=None) -> None:
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
            tenant_id=tenant_id,
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
        notification_service.notify(event="Alert Escalated", message=message, severity=alert.severity.value, tenant_id=tenant_id)
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
        notification_service.notify(event="Alert Escalated", message=message, severity=alert.severity.value, tenant_id=tenant_id)

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

    Tenant scoping: previously every enabled policy ran against every
    active alert fleet-wide, so one tenant's escalation policy could page
    a contact over another tenant's unacknowledged alert. A device-linked
    alert is now only evaluated against the global policies plus its own
    device's tenant's policies (see _policies_for_tenant); an alert with
    no device_id (some system-level alert sources set none) is only
    evaluated against global policies, since it has no tenant to resolve.
    """
    now = datetime.now(timezone.utc)
    all_policies = db.query(EscalationPolicy).filter(EscalationPolicy.enabled == True).all()
    if not all_policies:
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

    device_ids = {a.device_id for a in candidates if a.device_id}
    tenant_by_device = (
        {d.id: d.tenant_id for d in db.query(Device).filter(Device.id.in_(device_ids)).all()}
        if device_ids
        else {}
    )
    # Cache the resolved policy list per tenant_id (including None for
    # device-less alerts) so it's computed once per distinct tenant in
    # this sweep, not once per alert.
    policies_by_tenant: dict = {}

    fired = 0
    for alert in candidates:
        tenant_id = tenant_by_device.get(alert.device_id) if alert.device_id else None
        if tenant_id not in policies_by_tenant:
            policies_by_tenant[tenant_id] = _policies_for_tenant(all_policies, tenant_id)
        policies = policies_by_tenant[tenant_id]

        for policy in policies:
            if not _matches_scope(alert, policy.severity_scope.value):
                continue
            if not _due(alert, policy, now):
                continue

            _send(db, policy, alert, tenant_id=tenant_id)

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
                tenant_id=tenant_id,
            )
            fired += 1

    if fired:
        db.commit()
    return fired


def list_escalated_alerts(db: Session, limit: int = 100, tenant_id=None) -> list[Alert]:
    """tenant_id=None returns the fleet-wide feed (MSP staff / no scope);
    otherwise the feed is limited to alerts whose device belongs to that
    tenant. Alerts with no device_id (never had a tenant to begin with)
    are excluded once a tenant scope is applied, matching how they're
    excluded from run_escalation_sweep's per-tenant policy resolution.
    """
    q = db.query(Alert).filter(Alert.escalated == True)
    if tenant_id is not None:
        q = q.join(Device, Device.id == Alert.device_id).filter(Device.tenant_id == tenant_id)
    return (
        q.order_by(Alert.last_escalated_at.desc().nullslast(), Alert.escalated_at.desc())
        .limit(limit)
        .all()
    )
