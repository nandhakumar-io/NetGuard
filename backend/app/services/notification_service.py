"""Notification service (FR-11).

Every `notify(...)` call fans out to up to three channels:

  1. Slack / Microsoft Teams incoming webhooks (unchanged from the original
     prototype -- best-effort, swallows its own errors).
  2. Email via SMTP, using a small per-event-type template (subject line +
     body), skipped entirely when SMTP_HOST or NOTIFY_EMAIL_RECIPIENTS is
     unset.
  3. The in-app Notification Center: persists a `Notification` row (unless
     NOTIFICATIONS_INAPP_ENABLED is False) and publishes it on
     `event_bus.NOTIFICATIONS_CHANNEL` so every connected
     `/notifications/ws` client updates live (see app.api.notifications).

All three are best-effort and independent of each other: a failure in one
(Redis down, SMTP misconfigured, webhook unreachable) never blocks the
others and never blocks the caller's actual workflow (deployment pipeline,
drift sweep, health poll). Every failure is logged and swallowed, same
policy as event_bus.
"""
import json
import logging
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.core.database import SessionLocal
from app.services import event_bus

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 3.0


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    from_email: str
    use_tls: bool
    recipients: list[str]


def _smtp_config() -> SmtpConfig | None:
    """Resolves the active SMTP configuration, DB settings (Integrations
    page) taking priority over the SMTP_* env vars -- same priority order
    as every other DB-vs-env credential in this codebase (see
    app.services.credential_service). Returns None if email notifications
    aren't configured through either path, so callers can treat "no SMTP"
    as a normal, silent no-op rather than an error.
    """
    from app.models.notification_settings import SETTINGS_ROW_ID, NotificationSettings

    db = SessionLocal()
    try:
        db_settings = db.get(NotificationSettings, SETTINGS_ROW_ID)
        if db_settings is not None and db_settings.smtp_enabled and db_settings.smtp_host:
            recipients = [r.strip() for r in (db_settings.recipients or "").split(",") if r.strip()]
            if not recipients:
                return None
            password = None
            if db_settings.smtp_password_encrypted:
                password = crypto.decrypt(db_settings.smtp_password_encrypted)
            return SmtpConfig(
                host=db_settings.smtp_host,
                port=db_settings.smtp_port or 587,
                username=db_settings.smtp_username,
                password=password,
                from_email=db_settings.smtp_from_email or settings.SMTP_FROM_EMAIL,
                use_tls=db_settings.smtp_use_tls,
                recipients=recipients,
            )
    except Exception:
        logger.warning("Failed to read DB notification settings, falling back to env vars", exc_info=True)
    finally:
        db.close()

    if not settings.SMTP_HOST or not settings.NOTIFY_EMAIL_RECIPIENTS:
        return None
    recipients = [r.strip() for r in settings.NOTIFY_EMAIL_RECIPIENTS.split(",") if r.strip()]
    if not recipients:
        return None
    return SmtpConfig(
        host=settings.SMTP_HOST, port=settings.SMTP_PORT, username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD, from_email=settings.SMTP_FROM_EMAIL,
        use_tls=settings.SMTP_USE_TLS, recipients=recipients,
    )


def send_smtp(cfg: SmtpConfig, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """Low-level SMTP send shared by the notify() email channel, the
    compliance-report attachment sender, and the Integrations page's
    "Send test email" action -- one place that actually talks to
    smtplib so all three stay in sync on TLS/auth/timeout handling.
    Raises on failure; callers decide whether to swallow (best-effort
    notification) or surface (interactive test-send).
    """
    email_msg = EmailMessage()
    email_msg["Subject"] = subject
    email_msg["From"] = cfg.from_email
    email_msg["To"] = ", ".join(cfg.recipients)
    email_msg.set_content(body)
    for filename, data, subtype in attachments or []:
        email_msg.add_attachment(data, maintype="application", subtype=subtype, filename=filename)

    with smtplib.SMTP(cfg.host, cfg.port, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
        if cfg.use_tls:
            smtp.starttls()
        if cfg.username and cfg.password:
            smtp.login(cfg.username, cfg.password)
        smtp.send_message(email_msg)


def _post_webhook(url: str | None, payload: dict) -> None:
    if not url:
        return
    try:
        httpx.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Notification webhook post failed", exc_info=True)


# Maps a `notify(event=...)` label (as already used by pipeline_service,
# drift_service, metrics_service) to the coarse NotificationEventType the
# in-app center groups/icons by. Anything not listed here falls back to
# GENERIC -- callers are free to pass new event labels without needing a
# schema change.
_EVENT_TYPE_MAP = {
    "Deployment Succeeded": "deployment_succeeded",
    "Deployment Failed": "deployment_failed",
    "Automatic Rollback Triggered": "rollback_triggered",
    "Configuration Drift Detected": "drift_high",  # overridden to drift_critical below when severity is critical
}

# Email subject/intro templates, keyed by the same resolved event_type.
# `{message}` is substituted with the notification's message body.
_TEMPLATES = {
    "deployment_succeeded": ("[NetGuard] Deployment succeeded", "A configuration change deployed successfully.\n\n{message}"),
    "deployment_failed": ("[NetGuard] Deployment FAILED", "A configuration change failed to deploy.\n\n{message}"),
    "rollback_triggered": ("[NetGuard] Automatic rollback triggered", "Self-healing rollback ran after a failed health check.\n\n{message}"),
    "drift_high": ("[NetGuard] Configuration drift detected", "A device's configuration has drifted from its baseline.\n\n{message}"),
    "drift_critical": ("[NetGuard] CRITICAL configuration drift detected", "A device's configuration has critically drifted from its baseline.\n\n{message}"),
    "generic": ("[NetGuard] {event}", "{message}"),
}


def _resolve_event_type(event: str, severity: str) -> str:
    event_type = _EVENT_TYPE_MAP.get(event, "generic")
    if event_type == "drift_high" and severity == "critical":
        event_type = "drift_critical"
    return event_type


def send_email_attachment(
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
    recipients_override: list[str] | None = None,
) -> bool:
    """Send a standalone SMTP email with optional file attachments to
    NOTIFY_EMAIL_RECIPIENTS, using the same SMTP_* settings as `notify()`'s
    email channel. Unlike `notify()`, this does NOT also fan out to
    Slack/Teams/the in-app Notification Center -- it's for document
    deliverables (e.g. the scheduled compliance report PDF/CSV) rather than
    short event messages, and those channels aren't attachment-capable.

    attachments: list of (filename, raw_bytes, subtype) tuples, e.g.
    ("report.pdf", pdf_bytes, "pdf"). MIME maintype is always
    "application" (application/pdf, application/csv, etc.) -- fine for the
    document types this is used for.

    recipients_override: send to this list instead of the global
    NOTIFY_EMAIL_RECIPIENTS / NotificationSettings.recipients -- used by
    app.services.tenant_digest_service, where each subscription has its
    own delivery list rather than the operator-wide one. Still requires
    SMTP host/credentials to be configured globally; only the To: list is
    overridden. Ignored (falls back to the configured list) if empty.

    Returns True if the email was sent, False if it was skipped because
    SMTP_HOST isn't configured, or (when using the default recipient
    list) NOTIFY_EMAIL_RECIPIENTS isn't configured either. Never raises
    for a send failure -- logs and swallows it, same "notifications must
    never break the caller" policy as notify().
    """
    cfg = _smtp_config()
    if cfg is None:
        return False
    if recipients_override:
        from dataclasses import replace

        cfg = replace(cfg, recipients=recipients_override)
    elif not cfg.recipients:
        return False
    try:
        send_smtp(cfg, subject, body, attachments)
        return True
    except Exception:
        logger.warning("Compliance report email send failed", exc_info=True)
        return False


def _email_suppressed_by_tenant_digest(tenant_id, severity: str) -> bool:
    """Best-effort check of tenant_digest_service.is_live_suppressed --
    swallows its own errors (missing DB, bad tenant_id, etc.) and treats
    any failure as "don't suppress", same fail-open policy as the rest of
    this module: a digest-lookup problem should never be the reason a
    live alert email silently doesn't go out.
    """
    if tenant_id is None:
        return False
    db = SessionLocal()
    try:
        from app.services import tenant_digest_service

        return tenant_digest_service.is_live_suppressed(db, tenant_id, severity)
    except Exception:
        logger.warning("Tenant digest suppression check failed", exc_info=True)
        return False
    finally:
        db.close()


def _send_email(event_type: str, event: str, message: str) -> None:
    cfg = _smtp_config()
    if cfg is None:
        return

    subject_tpl, body_tpl = _TEMPLATES.get(event_type, _TEMPLATES["generic"])
    subject = subject_tpl.format(event=event, message=message)
    body = body_tpl.format(event=event, message=message)

    try:
        send_smtp(cfg, subject, body)
    except Exception:
        logger.warning("Notification email send failed", exc_info=True)


def _persist_and_broadcast(
    *,
    event_type: str,
    severity: str,
    title: str,
    message: str,
    device_hostname: str | None,
    change_request_id: uuid.UUID | None,
    deployment_id: uuid.UUID | None,
) -> None:
    if not settings.NOTIFICATIONS_INAPP_ENABLED:
        return

    # Import here (not at module load time) to avoid a circular import:
    # app.models.notification -> app.core.database -> ... this module is
    # imported from several services during app startup.
    from app.models.notification import Notification

    db: Session = SessionLocal()
    try:
        notification = Notification(
            event_type=event_type,
            severity=severity,
            title=title,
            message=message,
            device_hostname=device_hostname,
            change_request_id=change_request_id,
            deployment_id=deployment_id,
        )
        db.add(notification)
        db.commit()
        db.refresh(notification)

        event_bus.publish_event(
            "notification_created",
            channel=event_bus.NOTIFICATIONS_CHANNEL,
            id=str(notification.id),
            notification_event_type=notification.event_type.value if hasattr(notification.event_type, "value") else notification.event_type,
            severity=notification.severity.value if hasattr(notification.severity, "value") else notification.severity,
            title=notification.title,
            message=notification.message,
            device_hostname=notification.device_hostname,
            change_request_id=str(notification.change_request_id) if notification.change_request_id else None,
            deployment_id=str(notification.deployment_id) if notification.deployment_id else None,
            read=notification.read,
            created_at=notification.created_at.isoformat() if notification.created_at else None,
        )
    except Exception:
        logger.warning("Failed to persist/broadcast in-app notification", exc_info=True)
        db.rollback()
    finally:
        db.close()


def _post_telegram(event: str, message: str, severity: str) -> None:
    """Send a notification to the global Telegram chat configured via env vars."""
    bot_token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "resolved": "🟢"}.get(severity, "ℹ️")
    text = f"{emoji} <b>NetGuard — {event}</b>\n{message}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Telegram notification send failed", exc_info=True)


_ACTION_LABELS = {
    "acknowledge": "Acknowledge",
    "escalate": "Escalate",
    "run_runbook": "Run Runbook",
}


def _action_url(action: str, event_type: str | None, alert_id: str | None, runbook_id: str | None) -> str:
    """Deep link back into NetGuard for a given response action -- these
    are always plain "open this page, logged-in-user does the real
    thing" links rather than unauthenticated action endpoints, so a
    stolen/forwarded Slack message or push notification can't be used to
    acknowledge or escalate anything without a valid NetGuard session.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    if action == "run_runbook":
        if runbook_id:
            return f"{base}/alert-runbooks?runbook={runbook_id}"
        return f"{base}/alert-runbooks"
    if action == "escalate":
        if alert_id:
            return f"{base}/alerts?alert={alert_id}&action=escalate"
        return f"{base}/escalation-policies"
    # acknowledge
    if alert_id:
        return f"{base}/alerts?alert={alert_id}&action=acknowledge"
    return f"{base}/alerts"


def _build_action_buttons(
    actions: list[str] | None, event_type: str | None, alert_id: str | None, runbook_id: str | None
) -> list[dict]:
    """Common (label, url) pairs for the requested actions, shared by
    every webhook_type's button formatting below."""
    if not actions:
        return []
    out = []
    for action in actions:
        label = _ACTION_LABELS.get(action)
        if not label:
            continue
        out.append({"action": action, "label": label, "url": _action_url(action, event_type, alert_id, runbook_id)})
    return out


def _build_webhook_payload(
    wh_type: str,
    event: str,
    message: str,
    severity: str,
    event_type: str,
    actions: list[str] | None = None,
    alert_id: str | None = None,
    runbook_id: str | None = None,
) -> dict:
    """Formats the outbound JSON body for a given webhook type. Split out
    from _fan_out_user_webhooks so the exact same formatting logic backs
    a manual retry (app.api.webhooks.retry_delivery) -- a retry should
    resend what a fresh delivery of the same event would send today, not
    replay a stored payload that might be built from a since-removed
    field shape."""
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "resolved": "🟢"}.get(severity, "ℹ️")
    buttons = _build_action_buttons(actions, event_type, alert_id, runbook_id)
    if wh_type == "telegram":
        payload = {
            "chat_id": "",  # filled in by the caller, which has the WebhookEndpoint row
            "text": f"{emoji} <b>NetGuard — {event}</b>\n{message}",
            "parse_mode": "HTML",
        }
        if buttons:
            # Telegram inline keyboard: one URL button per row, so it's
            # never ambiguous which tap fires which action.
            payload["reply_markup"] = {"inline_keyboard": [[{"text": b["label"], "url": b["url"]}] for b in buttons]}
        return payload
    if wh_type == "slack":
        payload = {"text": f"{emoji} *NetGuard — {event}*\n{message}"}
        if buttons:
            payload["attachments"] = [
                {
                    "color": {"critical": "#dc2626", "warning": "#d97706"}.get(severity, "#0284c7"),
                    "actions": [
                        {"type": "button", "text": b["label"], "url": b["url"], "style": "danger" if b["action"] == "escalate" else "default"}
                        for b in buttons
                    ],
                }
            ]
        return payload
    if wh_type == "teams":
        payload = {"text": f"{emoji} **NetGuard — {event}**\n{message}"}
        if buttons:
            payload["potentialAction"] = [
                {"@type": "OpenUri", "name": b["label"], "targets": [{"os": "default", "uri": b["url"]}]}
                for b in buttons
            ]
        return payload
    return {
        "event": event,
        "event_type": event_type,
        "message": message,
        "severity": severity,
        "source": "netguard",
        "actions": buttons or None,
    }


def deliver_webhook(
    db: Session,
    wh,
    event: str,
    message: str,
    severity: str = "info",
    event_type: str | None = None,
    is_retry: bool = False,
    retry_of_id=None,
    retried_by: str | None = None,
    alert_id: str | None = None,
):
    """Sends one webhook delivery attempt and records it as a
    WebhookDeliveryAttempt row, whatever the outcome. Used both by the
    normal notify() fan-out (see _fan_out_user_webhooks below) and by the
    manual retry endpoint (POST /webhooks/deliveries/{id}/retry) -- the
    exact same send-and-log path either way, so a retried delivery is
    indistinguishable in the log from a fresh one except for is_retry/
    retry_of_id. Never raises: any failure (bad URL, timeout, non-2xx
    response) is captured into the logged row and returned, not thrown,
    since a webhook failure must never interrupt whatever workflow
    triggered the notification.
    """
    from app.models.webhook import WebhookDeliveryAttempt

    wh_type = wh.webhook_type.value if hasattr(wh.webhook_type, "value") else wh.webhook_type
    actions = None
    if getattr(wh, "include_actions", None):
        try:
            actions = json.loads(wh.include_actions)
        except (ValueError, TypeError):
            actions = None
    payload = _build_webhook_payload(
        wh_type, event, message, severity, event_type or event,
        actions=actions, alert_id=alert_id, runbook_id=str(wh.default_runbook_id) if getattr(wh, "default_runbook_id", None) else None,
    )
    if wh_type == "telegram":
        payload["chat_id"] = wh.telegram_chat_id or ""

    attempt = WebhookDeliveryAttempt(
        webhook_endpoint_id=wh.id,
        event=event,
        event_type=event_type,
        severity=severity,
        request_payload=json.dumps(payload),
        is_retry=is_retry,
        retry_of_id=retry_of_id,
        retried_by=retried_by,
    )

    try:
        resp = httpx.post(wh.url, json=payload, timeout=TIMEOUT_SECONDS)
        attempt.status_code = resp.status_code
        attempt.response_body = resp.text[:500] if resp.text else None
        attempt.success = resp.status_code < 400
        if not attempt.success:
            attempt.error = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 -- network/timeout/DNS/etc, all logged the same way
        attempt.success = False
        attempt.error = str(exc)[:500]

    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    if not attempt.success:
        logger.warning("User webhook delivery failed for %s: %s", wh.name, attempt.error)

    return attempt


def _fan_out_user_webhooks(
    event: str,
    message: str,
    severity: str,
    event_type: str,
    alert_id: str | None = None,
    tenant_id=None,
) -> None:
    """Send the notification to all enabled user-configured WebhookEndpoint rows.

    tenant_id: when given, only global (tenant_id IS NULL) webhooks and
    that tenant's own webhooks are notified -- previously this loaded
    every enabled WebhookEndpoint with no tenant filter at all, so an
    alert from one tenant's device could fan out to another tenant's
    Slack/Teams webhook. tenant_id=None (the caller has no device/tenant
    context -- e.g. a JIT-access or deployment-level event) reaches only
    global webhooks, same "global unless scoped" convention as the
    AlertRule engine.
    """
    import json as _json

    from app.models.webhook import WebhookEndpoint

    db = SessionLocal()
    try:
        q = db.query(WebhookEndpoint).filter(WebhookEndpoint.enabled == True)
        q = q.filter((WebhookEndpoint.tenant_id == tenant_id) | (WebhookEndpoint.tenant_id.is_(None)))
        webhooks = q.all()
        for wh in webhooks:
            # Check event subscription filter
            if wh.events:
                try:
                    subscribed = _json.loads(wh.events)
                    if isinstance(subscribed, list) and event_type not in subscribed:
                        continue
                except (ValueError, TypeError):
                    pass

            deliver_webhook(db, wh, event=event, message=message, severity=severity, event_type=event_type, alert_id=alert_id)
    except Exception:
        logger.warning("Failed to fan out to user webhooks", exc_info=True)
    finally:
        db.close()


def _fan_out_push(event: str, message: str, severity: str, alert_id: str | None = None) -> None:
    """Push to every enabled mobile/browser subscription via
    app.services.push_service (ntfy / Pushover / browser Web Push) --
    this was fully built (registration UI, delivery for all three
    providers, per-subscription severity filtering) but notify() never
    actually called it, so nothing ever reached a registered phone no
    matter how a subscription was configured. Same best-effort contract
    as every other channel here: never raises, never blocks the caller.
    """
    from app.services import push_service

    db = SessionLocal()
    try:
        push_service.send_push(db, title=f"NetGuard — {event}", message=message, severity=severity, alert_id=alert_id)
    except Exception:
        logger.warning("Failed to fan out push notifications", exc_info=True)
    finally:
        db.close()


def _fan_out_syslog(event: str, message: str, severity: str) -> None:
    """Forwards to every enabled RemoteSyslogDestination via
    app.services.syslog_forward_service. Same best-effort contract as
    every other channel here: never raises, never blocks the caller.
    """
    from app.services import syslog_forward_service

    db = SessionLocal()
    try:
        syslog_forward_service.fan_out(db, event, message, severity)
    except Exception:
        logger.warning("Failed to fan out remote syslog forwarding", exc_info=True)
    finally:
        db.close()


def notify(
    event: str,
    message: str,
    severity: str = "info",
    *,
    device_hostname: str | None = None,
    change_request_id: uuid.UUID | None = None,
    deployment_id: uuid.UUID | None = None,
    alert_id: uuid.UUID | str | None = None,
    tenant_id=None,
) -> None:
    """Fan out a notification to Slack, Teams, Telegram, Email, user-configured
    webhooks, and the in-app Notification Center.

    severity: "info" | "warning" | "critical" | "resolved" -- "resolved" is
        for a recovery notice (e.g. a down interface/device coming back
        up) and renders with a green check instead of the info/warning/
        critical icons, both here and on ntfy (see push_service).
    event: short human label (e.g. "Deployment Failed") -- also used to
        pick the in-app event_type/email template via _EVENT_TYPE_MAP.
    device_hostname / change_request_id / deployment_id: optional context
        surfaced in the in-app Notification Center so a notification can
        deep-link back to the device/change/deployment it's about.
    alert_id: when this notification is about a specific Alert, its id --
        lets webhook/push deliveries that opted into response action
        buttons (acknowledge/escalate/run runbook) deep-link straight to
        that alert instead of the general alerts list.
    tenant_id: the tenant this event is about, if any -- passed through to
        _fan_out_user_webhooks so a tenant's alert only reaches global or
        that tenant's own webhooks, never another tenant's. Callers with
        a device in scope should pass device.tenant_id; callers with no
        device/tenant context can omit it, which reaches global webhooks
        only. The global Slack/Teams/Telegram env-var channels are
        unaffected -- those are already operator-wide, not per-tenant.
        Email IS affected: if `tenant_id` has an active
        TenantDigestSubscription whose severity_floor covers this
        severity, the email leg below is held back and only shows up in
        that tenant's next digest -- see
        app.services.tenant_digest_service.is_live_suppressed. Slack/
        Teams/Telegram/in-app/webhooks all still fire immediately
        regardless; only the email channel is a digest's to gate, since
        it's the one a digest is meant to stand in for.
    """
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "resolved": "🟢"}.get(severity, "ℹ️")
    text = f"{emoji} *NetGuard — {event}*\n{message}"

    _post_webhook(settings.SLACK_WEBHOOK_URL, {"text": text})
    _post_webhook(settings.TEAMS_WEBHOOK_URL, {"text": text})

    event_type = _resolve_event_type(event, severity)

    if not _email_suppressed_by_tenant_digest(tenant_id, severity):
        _send_email(event_type, event, message)

    # Telegram (global env-var-based)
    _post_telegram(event, message, severity)

    alert_id_str = str(alert_id) if alert_id else None

    # User-configured webhooks (DB-based)
    _fan_out_user_webhooks(event, message, severity, event_type, alert_id=alert_id_str, tenant_id=tenant_id)

    # Mobile/browser push (ntfy, Pushover, Web Push)
    _fan_out_push(event, message, severity, alert_id=alert_id_str)

    # Remote syslog collectors (Splunk, Graylog, rsyslog, SIEM, ...)
    _fan_out_syslog(event, message, severity)

    _persist_and_broadcast(
        event_type=event_type,
        # NotificationSeverity (the in-app model's DB enum) only has
        # info/warning/critical -- it predates the "resolved" severity
        # added for recovery notices (interface/device back up), and
        # widening a Postgres enum type is its own migration. Store
        # "resolved" notices as "info" here (a real recovery event is
        # more interesting than routine info but not a warning) rather
        # than let the raw string hit the enum column and get this whole
        # in-app leg silently swallowed by the except below. Slack/
        # Teams/ntfy/webhooks above already got the real "resolved"
        # severity (green icon/tag), which is what actually matters for
        # "is this a recovery or a new problem" at a glance.
        severity="info" if severity == "resolved" else severity,
        title=event,
        message=message,
        device_hostname=device_hostname,
        change_request_id=change_request_id,
        deployment_id=deployment_id,
    )
