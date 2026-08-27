"""Per-tenant Alert/Incident/AuditLog digest subscriptions.

Same build -> render -> email shape as app.models.change_request_digest,
generalized to one row per (tenant, subscription) instead of one fixed
operator-wide weekly schedule -- see
app.models.tenant_digest_subscription for the full rationale.

Dispatch: app.tasks.run_tenant_digest_dispatch_task runs hourly and calls
subscriptions_due_at() + deliver_due_subscription() for whatever's due
this hour, rather than one Celery beat crontab entry per tenant.
"""
from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSeverity
from app.models.audit_log import AuditLog
from app.models.device import Device
from app.models.incident import Incident
from app.models.tenant import Tenant
from app.models.tenant_digest_subscription import (
    DigestCadence,
    DigestSeverityFloor,
    TenantDigestSubscription,
)

logger = logging.getLogger(__name__)

# Rank used both for "which alerts fall under this subscription's
# severity_floor" and for is_live_suppressed below -- higher is worse,
# matches app.services.alert_service._SEVERITY_RANK.
_SEVERITY_RANK = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
_FLOOR_RANK = {DigestSeverityFloor.ALL: -1, DigestSeverityFloor.WARNING: 1, DigestSeverityFloor.CRITICAL: 2}


def _device_ids_for_tenant(db: Session, tenant_id) -> list:
    return [d.id for d in db.query(Device.id).filter(Device.tenant_id == tenant_id).all()]


def subscriptions_due_at(db: Session, now: datetime.datetime) -> list[TenantDigestSubscription]:
    """Active subscriptions whose cadence/hour(/day) matches `now` (UTC).

    Called once per hour by the dispatcher task, so this only needs to
    match on the current hour -- day_of_week is only checked for WEEKLY.
    A subscription that's somehow missed for an hour (worker down, etc.)
    just goes out an hour late next time the dispatcher runs; nothing
    here tries to "catch up" missed hours, same tolerance the existing
    daily/weekly beat crontabs already have.
    """
    due = (
        db.query(TenantDigestSubscription)
        .filter(TenantDigestSubscription.is_active == True, TenantDigestSubscription.hour_utc == now.hour)
        .all()
    )
    return [
        sub
        for sub in due
        if sub.cadence == DigestCadence.DAILY
        or (sub.cadence == DigestCadence.WEEKLY and sub.day_of_week == now.weekday())
    ]


def is_live_suppressed(db: Session, tenant_id, severity: str) -> bool:
    """True if `severity` should be held for the digest instead of sent
    live via notification_service.notify's email channel, for the given
    tenant.

    A tenant can have several subscriptions with different floors (e.g.
    a daily critical-only one and a weekly full one) -- the *least*
    restrictive one wins, so adding a broader "send me everything weekly"
    subscription never accidentally silences live paging that a
    narrower/stricter subscription wasn't already suppressing.
    Tenant-less (tenant_id is None) events -- global/system alerts --
    are never suppressed; there's no subscription to hold them for.
    """
    if tenant_id is None:
        return False
    try:
        severity_enum = AlertSeverity(severity)
    except ValueError:
        return False

    subs = (
        db.query(TenantDigestSubscription)
        .filter(TenantDigestSubscription.tenant_id == tenant_id, TenantDigestSubscription.is_active == True)
        .all()
    )
    if not subs:
        return False

    floors = [_FLOOR_RANK[sub.severity_floor] for sub in subs]
    most_permissive_floor = min(floors)
    if most_permissive_floor < 0:
        return False  # at least one subscription is "all" -- never suppress
    return _SEVERITY_RANK[severity_enum] <= most_permissive_floor


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_digest(db: Session, sub: TenantDigestSubscription, now: datetime.datetime) -> dict:
    window_start = sub.last_sent_at or sub.created_at
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=datetime.timezone.utc)

    device_ids = _device_ids_for_tenant(db, sub.tenant_id)

    alerts: list[Alert] = []
    if device_ids:
        alerts = (
            db.query(Alert)
            .filter(Alert.device_id.in_(device_ids), Alert.created_at >= window_start)
            .order_by(Alert.created_at.asc())
            .all()
        )

    incidents: list[Incident] = []
    if alerts:
        root_alert_ids = {a.id for a in alerts}
        incidents = (
            db.query(Incident)
            .filter(Incident.created_at >= window_start, Incident.root_cause_alert_id.in_(root_alert_ids))
            .all()
        )

    audit_rows = (
        db.query(AuditLog)
        .filter(AuditLog.tenant_id == sub.tenant_id, AuditLog.created_at >= window_start)
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    by_severity: dict[str, int] = {}
    for a in alerts:
        key = a.severity.value if hasattr(a.severity, "value") else str(a.severity)
        by_severity[key] = by_severity.get(key, 0) + 1

    return {
        "window_start": window_start,
        "generated_at": now,
        "alerts": alerts,
        "by_severity": by_severity,
        "incidents": incidents,
        "audit_rows": audit_rows,
    }


def render_email(tenant: Tenant, sub: TenantDigestSubscription, digest: dict) -> tuple[str, str]:
    cadence_label = "Daily" if sub.cadence == DigestCadence.DAILY else "Weekly"
    subject = f"[NetGuard] {cadence_label} Digest -- {tenant.name} -- {digest['generated_at'].strftime('%Y-%m-%d')}"

    severity_lines = "\n".join(f"  {sev}: {count}" for sev, count in sorted(digest["by_severity"].items())) or "  (none)"

    alert_lines = "\n".join(
        f"  [{(a.severity.value if hasattr(a.severity, 'value') else a.severity)}] {a.category} -- {a.message[:120]}"
        for a in digest["alerts"][:50]
    ) or "  (none)"
    more_alerts = len(digest["alerts"]) - 50
    if more_alerts > 0:
        alert_lines += f"\n  ...and {more_alerts} more (see Alert Center for the full list)"

    incident_lines = "\n".join(f"  {i.title} [{i.status.value if hasattr(i.status, 'value') else i.status}]" for i in digest["incidents"]) or "  (none)"

    body = (
        f"{cadence_label} activity digest for {tenant.name}, "
        f"covering {digest['window_start'].strftime('%Y-%m-%d %H:%M UTC')} through "
        f"{digest['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
        f"Alerts by severity:\n{severity_lines}\n\n"
        f"Alerts:\n{alert_lines}\n\n"
        f"Incidents opened:\n{incident_lines}\n\n"
        f"Audit log entries: {len(digest['audit_rows'])}\n"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def deliver_due_subscription(db: Session, sub: TenantDigestSubscription) -> bool:
    """Builds, renders, and emails one subscription's digest to its own
    recipient list (NOT NotificationSettings/NOTIFY_EMAIL_RECIPIENTS --
    see TenantDigestSubscription.recipients), then advances
    last_sent_at regardless of whether the window had any activity, so
    the next digest doesn't re-report an empty window as "since
    creation".

    Returns True if the email was actually sent, False if it was skipped
    (SMTP not configured). Either way last_sent_at still advances and the
    attempt is recorded in the audit trail, same skip-but-record policy
    as change_request_digest.deliver_scheduled_digest.
    """
    from app.services import audit_service, notification_service

    tenant = db.get(Tenant, sub.tenant_id)
    if tenant is None:
        logger.warning("Tenant digest subscription %s references missing tenant %s", sub.id, sub.tenant_id)
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    digest = build_digest(db, sub, now)
    subject, body = render_email(tenant, sub, digest)

    recipients = [r.strip() for r in (sub.recipients or "").split(",") if r.strip()]
    sent = False
    if recipients:
        sent = notification_service.send_email_attachment(subject, body, attachments=None, recipients_override=recipients)

    sub.last_sent_at = now
    db.add(sub)
    db.commit()

    audit_service.record_event(
        db,
        actor="system:tenant-digest",
        action="Tenant Digest Delivery",
        result="Sent" if sent else "Skipped (SMTP not configured or no recipients)",
        detail=f"tenant={tenant.slug} subscription={sub.id} alerts={len(digest['alerts'])} incidents={len(digest['incidents'])}",
        tenant_id=sub.tenant_id,
    )
    return sent


def run_due_digests(db: Session, now: datetime.datetime | None = None) -> int:
    """Entry point for app.tasks.run_tenant_digest_dispatch_task. Returns
    the count of subscriptions processed (sent or skipped), not just sent
    -- matches how the compliance-report/change-request-digest tasks
    report their return value.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    due = subscriptions_due_at(db, now)
    for sub in due:
        try:
            deliver_due_subscription(db, sub)
        except Exception:
            logger.warning("Tenant digest delivery failed for subscription %s", sub.id, exc_info=True)
    return len(due)
