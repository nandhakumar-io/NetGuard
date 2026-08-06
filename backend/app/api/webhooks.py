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
from app.models.webhook import WebhookEndpoint
from app.schemas.webhook import (
    WebhookCreate,
    WebhookRead,
    WebhookTestResult,
    WebhookUpdate,
)

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
