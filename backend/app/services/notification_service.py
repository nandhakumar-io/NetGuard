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
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import event_bus

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 3.0


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


def _recipients() -> list[str]:
    if not settings.NOTIFY_EMAIL_RECIPIENTS:
        return []
    return [r.strip() for r in settings.NOTIFY_EMAIL_RECIPIENTS.split(",") if r.strip()]


def send_email_attachment(
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
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

    Returns True if the email was sent, False if it was skipped because
    SMTP_HOST or NOTIFY_EMAIL_RECIPIENTS isn't configured. Never raises for
    a send failure -- logs and swallows it, same "notifications must never
    break the caller" policy as notify().
    """
    recipients = _recipients()
    if not settings.SMTP_HOST or not recipients:
        return False

    email_msg = EmailMessage()
    email_msg["Subject"] = subject
    email_msg["From"] = settings.SMTP_FROM_EMAIL
    email_msg["To"] = ", ".join(recipients)
    email_msg.set_content(body)

    for filename, data, subtype in attachments or []:
        email_msg.add_attachment(data, maintype="application", subtype=subtype, filename=filename)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(email_msg)
        return True
    except Exception:
        logger.warning("Compliance report email send failed", exc_info=True)
        return False


def _send_email(event_type: str, event: str, message: str) -> None:
    if not settings.SMTP_HOST or not settings.NOTIFY_EMAIL_RECIPIENTS:
        return

    recipients = _recipients()
    if not recipients:
        return

    subject_tpl, body_tpl = _TEMPLATES.get(event_type, _TEMPLATES["generic"])
    subject = subject_tpl.format(event=event, message=message)
    body = body_tpl.format(event=event, message=message)

    email_msg = EmailMessage()
    email_msg["Subject"] = subject
    email_msg["From"] = settings.SMTP_FROM_EMAIL
    email_msg["To"] = ", ".join(recipients)
    email_msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(email_msg)
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
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    text = f"{emoji} <b>NetGuard — {event}</b>\n{message}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Telegram notification send failed", exc_info=True)


def _build_webhook_payload(wh_type: str, event: str, message: str, severity: str, event_type: str) -> dict:
    """Formats the outbound JSON body for a given webhook type. Split out
    from _fan_out_user_webhooks so the exact same formatting logic backs
    a manual retry (app.api.webhooks.retry_delivery) -- a retry should
    resend what a fresh delivery of the same event would send today, not
    replay a stored payload that might be built from a since-removed
    field shape."""
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    if wh_type == "telegram":
        return {
            "chat_id": "",  # filled in by the caller, which has the WebhookEndpoint row
            "text": f"{emoji} <b>NetGuard — {event}</b>\n{message}",
            "parse_mode": "HTML",
        }
    if wh_type == "slack":
        return {"text": f"{emoji} *NetGuard — {event}*\n{message}"}
    if wh_type == "teams":
        return {"text": f"{emoji} **NetGuard — {event}**\n{message}"}
    return {
        "event": event,
        "event_type": event_type,
        "message": message,
        "severity": severity,
        "source": "netguard",
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
    payload = _build_webhook_payload(wh_type, event, message, severity, event_type or event)
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


def _fan_out_user_webhooks(event: str, message: str, severity: str, event_type: str) -> None:
    """Send the notification to all enabled user-configured WebhookEndpoint rows."""
    import json as _json

    from app.models.webhook import WebhookEndpoint

    db = SessionLocal()
    try:
        webhooks = db.query(WebhookEndpoint).filter(WebhookEndpoint.enabled == True).all()
        for wh in webhooks:
            # Check event subscription filter
            if wh.events:
                try:
                    subscribed = _json.loads(wh.events)
                    if isinstance(subscribed, list) and event_type not in subscribed:
                        continue
                except (ValueError, TypeError):
                    pass

            deliver_webhook(db, wh, event=event, message=message, severity=severity, event_type=event_type)
    except Exception:
        logger.warning("Failed to fan out to user webhooks", exc_info=True)
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
) -> None:
    """Fan out a notification to Slack, Teams, Telegram, Email, user-configured
    webhooks, and the in-app Notification Center.

    severity: "info" | "warning" | "critical"
    event: short human label (e.g. "Deployment Failed") -- also used to
        pick the in-app event_type/email template via _EVENT_TYPE_MAP.
    device_hostname / change_request_id / deployment_id: optional context
        surfaced in the in-app Notification Center so a notification can
        deep-link back to the device/change/deployment it's about.
    """
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    text = f"{emoji} *NetGuard — {event}*\n{message}"

    _post_webhook(settings.SLACK_WEBHOOK_URL, {"text": text})
    _post_webhook(settings.TEAMS_WEBHOOK_URL, {"text": text})

    event_type = _resolve_event_type(event, severity)

    _send_email(event_type, event, message)

    # Telegram (global env-var-based)
    _post_telegram(event, message, severity)

    # User-configured webhooks (DB-based)
    _fan_out_user_webhooks(event, message, severity, event_type)

    _persist_and_broadcast(
        event_type=event_type,
        severity=severity,
        title=event,
        message=message,
        device_hostname=device_hostname,
        change_request_id=change_request_id,
        deployment_id=deployment_id,
    )
