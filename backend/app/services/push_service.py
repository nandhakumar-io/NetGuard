"""Mobile push notifications for on-call engineers.

Deliberately built on ntfy/Pushover (plain HTTP POST to an existing
mobile app) rather than a NetGuard-authored iOS/Android app with its own
APNs/FCM registration -- that's a much bigger lift for the same outcome
("this device's lock screen buzzes"), and both providers have free
mobile apps an engineer can install today. See app.models.push_subscription
for why `target` doesn't need encryption at rest.

Every send is best-effort per subscription: one engineer's misconfigured
topic/key never blocks delivery to anyone else's, matching the existing
"notifications must never break the caller" policy used throughout
notification_service.
"""
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.push_subscription import PushSubscription

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0

# ntfy priority levels: 1 min .. 5 max. "urgent" (5) bypasses the phone's
# silent/DND mode on supported ntfy clients, which is exactly what a P1
# page needs at 3am.
_NTFY_PRIORITY = {"critical": "urgent", "warning": "high", "info": "default"}
_PUSHOVER_PRIORITY = {"critical": 2, "warning": 1, "info": 0}  # 2 = emergency (repeats until acked)

_ACTION_LABELS = {"acknowledge": "Acknowledge", "escalate": "Escalate", "run_runbook": "Run Runbook"}


def _action_deep_link(action: str, alert_id: str | None) -> str:
    """Same deep-link scheme as notification_service._action_url -- these
    always open the NetGuard UI (which requires a real session) rather
    than firing a bare unauthenticated action, so a lost/shared phone
    notification can't be used to ack or escalate anything on its own.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    if action == "run_runbook":
        return f"{base}/alert-runbooks"
    if action == "escalate":
        return f"{base}/alerts?alert={alert_id}&action=escalate" if alert_id else f"{base}/escalation-policies"
    return f"{base}/alerts?alert={alert_id}&action=acknowledge" if alert_id else f"{base}/alerts"


def _ntfy_actions_header(include_actions: str | None, alert_id: str | None) -> str | None:
    """Builds the value of ntfy's `Actions` header: a comma-separated list
    of `view, Label, url` triples, which ntfy renders as up to three tap
    targets under the notification. See https://ntfy.sh/docs/publish/#action-buttons
    """
    if not include_actions:
        return None
    try:
        actions = json.loads(include_actions)
    except (ValueError, TypeError):
        return None
    if not actions:
        return None
    parts = []
    for action in actions[:3]:  # ntfy supports at most 3 action buttons
        label = _ACTION_LABELS.get(action)
        if not label:
            continue
        url = _action_deep_link(action, alert_id)
        parts.append(f"view, {label}, {url}")
    return "; ".join(parts) if parts else None


def _send_ntfy(sub: PushSubscription, title: str, message: str, severity: str, url: str | None, alert_id: str | None = None) -> bool:
    headers = {
        "Title": title,
        "Priority": _NTFY_PRIORITY.get(severity, "default"),
        "Tags": "rotating_light" if severity == "critical" else "warning",
    }
    if url:
        headers["Click"] = url
    actions_header = _ntfy_actions_header(getattr(sub, "include_actions", None), alert_id)
    if actions_header:
        headers["Actions"] = actions_header
    if severity == "critical":
        # Emergency-priority ntfy pushes retry/repeat client-side until
        # acknowledged in the app, same intent as Pushover priority 2
        # below -- a P1 shouldn't be silently missed because one push
        # arrived while the phone was locked in a pocket.
        headers["Priority"] = "urgent"
    try:
        resp = httpx.post(sub.target, content=message.encode("utf-8"), headers=headers, timeout=TIMEOUT_SECONDS)
        return resp.status_code < 300
    except Exception:
        logger.warning("ntfy push failed for subscription %s", sub.id, exc_info=True)
        return False


def _send_pushover(sub: PushSubscription, title: str, message: str, severity: str, url: str | None) -> bool:
    if not settings.PUSHOVER_APP_TOKEN:
        logger.warning("Pushover subscription %s configured but PUSHOVER_APP_TOKEN is unset", sub.id)
        return False
    payload = {
        "token": settings.PUSHOVER_APP_TOKEN,
        "user": sub.target,
        "title": title,
        "message": message,
        "priority": _PUSHOVER_PRIORITY.get(severity, 0),
    }
    if payload["priority"] == 2:
        # Emergency priority requires retry/expire -- retry the alert
        # every 60s for up to 1 hour until the engineer acknowledges it
        # in the Pushover app.
        payload["retry"] = 60
        payload["expire"] = 3600
    if url:
        payload["url"] = url
        payload["url_title"] = "Open in NetGuard"
    try:
        resp = httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=TIMEOUT_SECONDS)
        return resp.status_code < 300
    except Exception:
        logger.warning("Pushover push failed for subscription %s", sub.id, exc_info=True)
        return False


def _send_browser(sub: PushSubscription, title: str, message: str, severity: str, url: str | None) -> bool:
    """Web Push (VAPID) delivery straight to the browser -- no mobile app
    involved. `sub.target` holds the JSON {endpoint, p256dh, auth} captured
    when the browser subscribed (see app.api.push_subscriptions), matching
    the shape the browser's own PushSubscription.toJSON() produces. The
    frontend's /sw.js service worker turns the resulting push event into
    an OS-level notification.

    Best-effort like every other provider here: a browser subscription
    that's gone stale (browser data cleared, permission revoked, endpoint
    expired) makes the push service respond 404/410 -- we disable the
    subscription so it stops being retried on every future alert, the
    same "quietly stop bothering a dead target" behavior a mobile OS's
    own push service gives ntfy/Pushover for free.
    """
    if not settings.VAPID_PUBLIC_KEY or not settings.VAPID_PRIVATE_KEY:
        logger.warning("Browser push subscription %s exists but VAPID keys are unset", sub.id)
        return False
    try:
        subscription_info = json.loads(sub.target)
    except (TypeError, ValueError):
        logger.warning("Browser push subscription %s has malformed target JSON", sub.id)
        return False

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush not installed -- cannot deliver browser push for subscription %s", sub.id)
        return False

    payload = json.dumps(
        {
            "title": title,
            "body": message,
            "severity": severity,
            "url": url,
        }
    )
    try:
        webpush(
            subscription_info={
                "endpoint": subscription_info["endpoint"],
                "keys": {
                    "p256dh": subscription_info["p256dh"],
                    "auth": subscription_info["auth"],
                },
            },
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.VAPID_CONTACT_EMAIL}"},
            timeout=TIMEOUT_SECONDS,
        )
        return True
    except WebPushException as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code in (404, 410):
            # Push service says this endpoint is gone for good (browser
            # unsubscribed, data cleared, etc.) -- disable rather than
            # fail silently forever on every future alert.
            sub.enabled = False
        logger.warning("Browser push failed for subscription %s: %s", sub.id, exc, exc_info=True)
        return False
    except Exception:
        logger.warning("Browser push failed for subscription %s", sub.id, exc_info=True)
        return False


def _send_one(sub: PushSubscription, title: str, message: str, severity: str, url: str | None, alert_id: str | None = None) -> bool:
    provider = sub.provider.value if hasattr(sub.provider, "value") else str(sub.provider)
    if provider == "pushover":
        return _send_pushover(sub, title, message, severity, url)
    if provider == "browser":
        return _send_browser(sub, title, message, severity, url)
    return _send_ntfy(sub, title, message, severity, url, alert_id=alert_id)


def send_push(
    db: Session,
    *,
    title: str,
    message: str,
    severity: str = "critical",
    url: str | None = None,
    user_ids: list | None = None,
    alert_id: str | None = None,
) -> int:
    """Pushes to every enabled subscription that opted into this
    severity -- subscriptions default to critical-only (see
    PushSubscription.include_non_critical), so a P1 always reaches every
    registered phone while routine warnings only reach devices that
    explicitly asked for them.

    `user_ids`: restrict delivery to specific users' subscriptions (e.g.
    an escalation policy's designated on-call engineer) instead of every
    subscription in the system. None = fan out to everyone subscribed.

    `alert_id`: when this push is about a specific Alert, its id -- used
    to build the acknowledge/escalate/run-runbook action buttons (ntfy
    only, see _ntfy_actions_header) so they deep-link to that alert.

    Returns the number of subscriptions successfully pushed to.
    """
    query = db.query(PushSubscription).filter(PushSubscription.enabled == True)  # noqa: E712
    if user_ids:
        query = query.filter(PushSubscription.user_id.in_(user_ids))
    subs = query.all()

    sent = 0
    now = datetime.now(timezone.utc)
    for sub in subs:
        if severity != "critical" and not sub.include_non_critical:
            continue
        if _send_one(sub, title, message, severity, url, alert_id=alert_id):
            sub.last_pushed_at = now
            db.add(sub)
            sent += 1

    if sent:
        db.commit()
    return sent


def send_test_push(sub: PushSubscription) -> bool:
    """Fires a one-off test push to a single subscription, independent
    of the severity-filtering logic in send_push -- a test should always
    go through regardless of include_non_critical, so the button in the
    UI reliably confirms the subscription is wired up correctly.
    """
    return _send_one(
        sub,
        title="NetGuard test push",
        message="If you can see this, push notifications are working for this device.",
        severity="critical",
        url=None,
    )
