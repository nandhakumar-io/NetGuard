"""Webhook Endpoints CRUD API — user-configurable notification delivery targets.

  GET     /webhooks          — list all webhook endpoints
  POST    /webhooks          — create a new webhook endpoint
  PUT     /webhooks/{id}     — update an endpoint
  DELETE  /webhooks/{id}     — delete an endpoint
  POST    /webhooks/{id}/test — send a test payload to an endpoint
"""
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.webhook import WebhookDeliveryAttempt, WebhookEndpoint
from app.schemas.webhook import (
    WebhookCreate,
    WebhookDeliveryRead,
    WebhookRead,
    WebhookTestResult,
    WebhookUpdate,
)
from app.services.notification_service import deliver_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _webhook_to_read(wh: WebhookEndpoint) -> WebhookRead:
    """Convert a WebhookEndpoint ORM row to a WebhookRead, parsing events JSON."""
    events = None
    if wh.events:
        try:
            events = json.loads(wh.events)
        except (ValueError, TypeError):
            events = None
    return WebhookRead(
        id=wh.id,
        name=wh.name,
        url=wh.url,
        webhook_type=wh.webhook_type.value if hasattr(wh.webhook_type, "value") else wh.webhook_type,
        secret=wh.secret,
        events=events,
        telegram_chat_id=wh.telegram_chat_id,
        enabled=wh.enabled,
        created_by=wh.created_by,
        created_at=wh.created_at,
        updated_at=wh.updated_at,
    )


@router.get("", response_model=list[WebhookRead])
def list_webhooks(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = db.query(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc()).limit(limit).all()
    return [_webhook_to_read(r) for r in rows]


@router.post("", response_model=WebhookRead, status_code=201)
def create_webhook(
    body: WebhookCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    wh = WebhookEndpoint(
        name=body.name,
        url=body.url,
        webhook_type=body.webhook_type,
        secret=body.secret,
        events=json.dumps(body.events) if body.events else None,
        telegram_chat_id=body.telegram_chat_id,
        enabled=body.enabled,
        created_by=user.email,
    )
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return _webhook_to_read(wh)


@router.put("/{webhook_id}", response_model=WebhookRead)
def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    wh = db.get(WebhookEndpoint, webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    updates = body.model_dump(exclude_unset=True)
    if "events" in updates:
        updates["events"] = json.dumps(updates["events"]) if updates["events"] else None
    for field, value in updates.items():
        setattr(wh, field, value)
    db.commit()
    db.refresh(wh)
    return _webhook_to_read(wh)


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    wh = db.get(WebhookEndpoint, webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    db.delete(wh)
    db.commit()


@router.post("/{webhook_id}/test", response_model=WebhookTestResult)
def test_webhook(
    webhook_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    wh = db.get(WebhookEndpoint, webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")

    test_payload = {
        "event": "webhook_test",
        "message": "🔔 This is a test notification from NetGuard.",
        "severity": "info",
        "source": "netguard",
    }

    try:
        wh_type = wh.webhook_type.value if hasattr(wh.webhook_type, "value") else wh.webhook_type
        if wh_type == "telegram":
            chat_id = wh.telegram_chat_id or ""
            tg_payload = {
                "chat_id": chat_id,
                "text": "🔔 <b>NetGuard — Webhook Test</b>\nThis is a test notification from NetGuard.",
                "parse_mode": "HTML",
            }
            resp = httpx.post(wh.url, json=tg_payload, timeout=5.0)
        elif wh_type == "slack":
            resp = httpx.post(wh.url, json={"text": "🔔 *NetGuard — Webhook Test*\nThis is a test notification from NetGuard."}, timeout=5.0)
        elif wh_type == "teams":
            resp = httpx.post(wh.url, json={"text": "🔔 **NetGuard — Webhook Test**\nThis is a test notification from NetGuard."}, timeout=5.0)
        else:
            resp = httpx.post(wh.url, json=test_payload, timeout=5.0)

        return WebhookTestResult(
            success=resp.status_code < 400,
            message=f"HTTP {resp.status_code}" if resp.status_code < 400 else f"HTTP {resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
        )
    except Exception as exc:
        return WebhookTestResult(success=False, message=str(exc)[:300])


def _delivery_to_read(attempt: WebhookDeliveryAttempt, webhook_name: str | None = None) -> WebhookDeliveryRead:
    return WebhookDeliveryRead(
        id=attempt.id,
        webhook_endpoint_id=attempt.webhook_endpoint_id,
        webhook_endpoint_name=webhook_name or (attempt.webhook_endpoint.name if attempt.webhook_endpoint else None),
        event=attempt.event,
        event_type=attempt.event_type,
        severity=attempt.severity,
        success=attempt.success,
        status_code=attempt.status_code,
        response_body=attempt.response_body,
        error=attempt.error,
        is_retry=attempt.is_retry,
        retry_of_id=attempt.retry_of_id,
        retried_by=attempt.retried_by,
        attempted_at=attempt.attempted_at,
    )


@router.get("/deliveries", response_model=list[WebhookDeliveryRead])
def list_all_deliveries(
    limit: int = Query(100, ge=1, le=500),
    success: bool | None = Query(None, description="Filter to only successful (true) or only failed (false) attempts."),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Recent delivery attempts across every webhook endpoint, newest
    first -- backs the top-level delivery log view (as opposed to
    GET /webhooks/{id}/deliveries, scoped to one endpoint)."""
    q = db.query(WebhookDeliveryAttempt).order_by(WebhookDeliveryAttempt.attempted_at.desc())
    if success is not None:
        q = q.filter(WebhookDeliveryAttempt.success == success)
    rows = q.limit(limit).all()
    return [_delivery_to_read(r) for r in rows]


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryRead])
def list_webhook_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    wh = db.get(WebhookEndpoint, webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    rows = (
        db.query(WebhookDeliveryAttempt)
        .filter(WebhookDeliveryAttempt.webhook_endpoint_id == webhook_id)
        .order_by(WebhookDeliveryAttempt.attempted_at.desc())
        .limit(limit)
        .all()
    )
    return [_delivery_to_read(r, webhook_name=wh.name) for r in rows]


@router.post("/deliveries/{attempt_id}/retry", response_model=WebhookDeliveryRead)
def retry_delivery(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manually resends the same event to the same webhook endpoint,
    logging a brand-new WebhookDeliveryAttempt row (is_retry=True,
    retry_of_id pointing at the original) rather than mutating the
    original row in place -- the original attempt is a historical fact
    ("this failed at this time") that a later success shouldn't erase
    from the log.

    404s if the original attempt is gone; 409s if the webhook endpoint
    it targeted has since been deleted (nothing to resend to) or
    disabled (retrying would silently violate the user's own "don't
    send to this endpoint" setting).
    """
    original = db.get(WebhookDeliveryAttempt, attempt_id)
    if not original:
        raise HTTPException(status_code=404, detail="Delivery attempt not found")

    wh = db.get(WebhookEndpoint, original.webhook_endpoint_id)
    if not wh:
        raise HTTPException(status_code=409, detail="The webhook endpoint this was sent to no longer exists")
    if not wh.enabled:
        raise HTTPException(status_code=409, detail="This webhook endpoint is disabled -- enable it before retrying")

    new_attempt = deliver_webhook(
        db, wh,
        event=original.event,
        message=_reconstruct_message(original),
        severity=original.severity or "info",
        event_type=original.event_type,
        is_retry=True,
        retry_of_id=original.id,
        retried_by=user.email,
    )
    return _delivery_to_read(new_attempt, webhook_name=wh.name)


def _reconstruct_message(original: WebhookDeliveryAttempt) -> str:
    """Recovers the plain-text message for a retry from the originally
    logged request_payload, since WebhookDeliveryAttempt doesn't store
    the raw `message` separately from the fully-formatted per-type
    payload. Falls back to the stored event title if the payload can't
    be parsed (e.g. a row from before this column existed)."""
    if original.request_payload:
        try:
            body = json.loads(original.request_payload)
            text = body.get("text") or body.get("message")
            if text:
                # Slack/Teams/Telegram formatting wraps the message with
                # an emoji + "NetGuard — <event>\n" header -- strip that
                # back off so a retry re-wraps it fresh rather than
                # double-wrapping.
                marker = "\n"
                if marker in text:
                    return text.split(marker, 1)[1]
                return text
        except (ValueError, TypeError, AttributeError):
            pass
    return original.event
