"""Notification service (FR-11).

Sends best-effort notifications for key deployment events to Slack and
Microsoft Teams via incoming webhooks. Failures to notify never block the
deployment/rollback workflow -- they are logged and swallowed.
"""
import httpx

from app.core.config import settings

TIMEOUT_SECONDS = 3.0


def _post_webhook(url: str | None, payload: dict) -> None:
    if not url:
        return
    try:
        httpx.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - notifications must never break the pipeline
        pass


def notify(event: str, message: str, severity: str = "info") -> None:
    """Fan out a notification to all configured channels.

    severity: "info" | "warning" | "critical"
    """
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    text = f"{emoji} *NetGuard — {event}*\n{message}"

    _post_webhook(settings.SLACK_WEBHOOK_URL, {"text": text})
    _post_webhook(settings.TEAMS_WEBHOOK_URL, {"text": text})
