"""In-app Notification Center API + realtime WebSocket (SRS FR-11).

REST:
  GET    /notifications              — filtered list (severity, event_type, unread_only)
  GET    /notifications/summary      — unread_count + total, for the bell badge
  PATCH  /notifications/{id}/read    — mark one notification read
  PATCH  /notifications/read-all     — mark every notification read

WebSocket:
  WS     /notifications/ws           — pushes every notification event in realtime

Mirrors app.api.alerts's REST + WebSocket shape (same event_bus /
SessionLocal pattern), since Notifications and Alerts are both flat,
realtime activity feeds -- just backed by different tables.
"""
import asyncio
import contextlib
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user, get_current_user_ws
from app.models.notification import (
    Notification,
    NotificationEventType,
    NotificationSeverity,
)
from app.models.user import User
from app.schemas.notifications import NotificationRead, NotificationSummary
from app.services import event_bus

router = APIRouter(prefix="/notifications", tags=["notifications"])

HEARTBEAT_INTERVAL_SECONDS = 30


# ------------------------------------------------------------------
# REST
# ------------------------------------------------------------------
@router.get("", response_model=list[NotificationRead])
def list_notifications(
    severity: str | None = None,
    event_type: str | None = None,
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(Notification)
    if severity:
        q = q.filter(Notification.severity == NotificationSeverity(severity))
    if event_type:
        q = q.filter(Notification.event_type == NotificationEventType(event_type))
    if unread_only:
        q = q.filter(Notification.read == False)
    return q.order_by(desc(Notification.created_at)).offset(offset).limit(limit).all()


@router.get("/summary", response_model=NotificationSummary)
def get_summary(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    total = db.query(Notification).count()
    unread_count = db.query(Notification).filter(Notification.read == False).count()
    return NotificationSummary(unread_count=unread_count, total=total)


@router.patch("/read-all", response_model=NotificationSummary)
def mark_all_read(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    db.query(Notification).filter(Notification.read == False).update({"read": True})
    db.commit()
    total = db.query(Notification).count()
    event_bus.publish_event("notifications_read_all", channel=event_bus.NOTIFICATIONS_CHANNEL)
    return NotificationSummary(unread_count=0, total=total)


@router.patch("/{notification_id}/read", response_model=NotificationRead)
def mark_read(notification_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not notification.read:
        notification.read = True
        db.commit()
        db.refresh(notification)
        event_bus.publish_event(
            "notification_read", channel=event_bus.NOTIFICATIONS_CHANNEL, id=str(notification.id)
        )
    return notification


# ------------------------------------------------------------------
# WebSocket — realtime notification stream
# ------------------------------------------------------------------
def _serialize_recent(db: Session, n: int = 20) -> list[dict]:
    rows = db.query(Notification).order_by(desc(Notification.created_at)).limit(n).all()
    result = []
    for note in rows:
        result.append({
            "id": str(note.id),
            "event_type": note.event_type.value if note.event_type else "generic",
            "severity": note.severity.value if note.severity else "info",
            "title": note.title,
            "message": note.message,
            "device_hostname": note.device_hostname,
            "change_request_id": str(note.change_request_id) if note.change_request_id else None,
            "deployment_id": str(note.deployment_id) if note.deployment_id else None,
            "read": note.read,
            "created_at": note.created_at.isoformat() if note.created_at else None,
        })
    return result


def _summary(db: Session) -> dict:
    total = db.query(Notification).count()
    unread_count = db.query(Notification).filter(Notification.read == False).count()
    return {"unread_count": unread_count, "total": total}


async def _heartbeat_loop(websocket: WebSocket):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send_json({"type": "heartbeat"})
        except Exception:
            break


@router.websocket("/ws")
async def notifications_ws(websocket: WebSocket, token: str = Query("")):
    # Was accepting every connection unauthenticated -- see
    # app.api.dashboard.dashboard_ws for the same fix and why.
    db = SessionLocal()
    try:
        user = get_current_user_ws(token, db)
    finally:
        db.close()
    if not user:
        await websocket.close(code=1008)  # Policy Violation
        return

    await websocket.accept()

    db = SessionLocal()
    try:
        recent = _serialize_recent(db)
        summary = _summary(db)
        await websocket.send_json({"type": "initial", "notifications": recent, "summary": summary})
    finally:
        db.close()

    redis_client = event_bus.get_async_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(event_bus.NOTIFICATIONS_CHANNEL)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None:
                continue
            db = SessionLocal()
            try:
                recent = _serialize_recent(db)
                summary = _summary(db)
                await websocket.send_json({"type": "update", "notifications": recent, "summary": summary})
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pubsub.unsubscribe(event_bus.NOTIFICATIONS_CHANNEL)
        await pubsub.close()
        await redis_client.close()
