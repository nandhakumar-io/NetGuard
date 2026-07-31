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
    except Exception:  # noqa: BLE001 - notifications must never break the pipeline
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
    except Exception:  # noqa: BLE001 - email must never break the caller
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
    except Exception:  # noqa: BLE001 - email must never break the pipeline
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
    except Exception:  # noqa: BLE001 - in-app notification center must never break the pipeline
        logger.warning("Failed to persist/broadcast in-app notification", exc_info=True)
        db.rollback()
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
    """Fan out a notification to Slack, Teams, Email, and the in-app
    Notification Center.

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

    _persist_and_broadcast(
        event_type=event_type,
        severity=severity,
        title=event,
        message=message,
        device_hostname=device_hostname,
        change_request_id=change_request_id,
        deployment_id=deployment_id,
    )