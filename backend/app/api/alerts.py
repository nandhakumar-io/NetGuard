"""Alert API endpoints + realtime WebSocket.

REST:
  GET    /alerts           — filtered list (severity, source, status, device_id)
  GET    /alerts/summary   — counts by severity for dashboard stat cards
  GET    /alerts/{id}      — single alert
  PATCH  /alerts/{id}/acknowledge
  PATCH  /alerts/{id}/resolve
  POST   /alerts/clear      — bulk-resolve (kept for audit trail)
  DELETE /alerts/clear      — bulk hard-delete (removes rows entirely)

WebSocket:
  WS     /alerts/ws        — pushes every alert event in realtime
"""
import asyncio
import contextlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user
from app.models.alert import Alert, AlertSeverity, AlertSource
from app.models.user import User
from app.schemas.alert import AlertRead, AlertSummary
from app.services import alert_service, alert_snooze_service, event_bus

router = APIRouter(prefix="/alerts", tags=["alerts"])

HEARTBEAT_INTERVAL_SECONDS = 30


# ------------------------------------------------------------------
# REST
# ------------------------------------------------------------------
@router.get("", response_model=list[AlertRead])
def list_alerts(
    severity: str | None = None,
    source: str | None = None,
    status: str | None = Query(None, description="active | acknowledged | resolved"),
    device_id: uuid.UUID | None = None,
    suppressed: bool | None = Query(
        None, description="Filter by topology-correlation suppression: true=only impacted, false=only root-cause/standalone"
    ),
    in_maintenance: bool | None = Query(
        None,
        description="Filter by maintenance-window suppression: true=only alerts raised during a maintenance window, "
        "false=only alerts not covered by one. Omit to include both.",
    ),
    muted: bool | None = Query(
        None,
        description="Filter by an active (non-expired) snooze: true=only currently-muted alerts, "
        "false=only alerts with no active mute. Omit to include both.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(Alert)
    if severity:
        q = q.filter(Alert.severity == AlertSeverity(severity))
    if source:
        q = q.filter(Alert.source == AlertSource(source))
    if suppressed is not None:
        q = q.filter(Alert.suppressed == suppressed)
    if in_maintenance is not None:
        if in_maintenance:
            q = q.filter(Alert.suppressed_by_window_id.isnot(None))
        else:
            q = q.filter(Alert.suppressed_by_window_id.is_(None))
    if status == "active":
        q = q.filter(Alert.resolved == False)  # noqa: E712
    elif status == "acknowledged":
        q = q.filter(Alert.acknowledged == True, Alert.resolved == False)  # noqa: E712
    elif status == "resolved":
        q = q.filter(Alert.resolved == True)  # noqa: E712
    if device_id:
        q = q.filter(Alert.device_id == device_id)
    results = q.order_by(desc(Alert.created_at)).offset(offset).limit(limit).all()

    # muted_until is computed (not trusted straight off the possibly-
    # stale muted_by_snooze_id FK -- see AlertRead.muted_until's
    # docstring), so it's attached here rather than left to the ORM ->
    # Pydantic conversion, and the `muted` filter (which needs the same
    # "is this snooze still actually active" check) is applied after.
    mute_map = alert_snooze_service.active_mute_map(db, results)
    read_results = []
    for alert in results:
        obj = AlertRead.model_validate(alert)
        obj.muted_until = mute_map.get(alert.id)
        if muted is not None and (obj.muted_until is not None) != muted:
            continue
        read_results.append(obj)
    return read_results


@router.get("/summary", response_model=AlertSummary)
def get_summary(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return alert_service.get_alert_summary(db)


@router.get("/{alert_id}", response_model=AlertRead)
def get_alert(alert_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    obj = AlertRead.model_validate(alert)
    obj.muted_until = alert_snooze_service.active_mute_map(db, [alert]).get(alert.id)
    return obj


@router.patch("/{alert_id}/acknowledge", response_model=AlertRead)
def acknowledge(alert_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return alert_service.acknowledge_alert(db, alert_id, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{alert_id}/resolve", response_model=AlertRead)
def resolve(alert_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return alert_service.resolve_alert(db, alert_id, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/clear")
def clear_alerts(
    device_id: uuid.UUID | None = Query(None, description="Only clear alerts for this device; omit to clear everything"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resolve every currently-active alert in one call (the 'Clear
    Alerts' button on the Alert Center / device Alerts tab). Alerts are
    resolved, not deleted, so they still show up under the 'resolved'
    filter and in the audit trail.
    """
    cleared = alert_service.clear_alerts(db, user.email, device_id=device_id)
    return {"cleared": cleared}


@router.delete("/clear")
def purge_alerts(
    device_id: uuid.UUID | None = Query(None, description="Only delete alerts for this device; omit to delete everything"),
    only_active: bool = Query(
        False, description="If true, only delete unresolved alerts; if false (default), delete every matching alert including resolved history"
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Permanently remove alerts (the 'Clear Alerts' button on the Alert
    Center / device Alerts tab) instead of just marking them resolved.
    Rows are deleted outright, so cleared alerts no longer appear anywhere
    -- including the 'resolved' filter -- unlike POST /alerts/clear.
    """
    purged = alert_service.purge_alerts(db, device_id=device_id, only_active=only_active)
    return {"purged": purged}


# ------------------------------------------------------------------
# WebSocket — realtime alert event stream
# ------------------------------------------------------------------
def _serialize_recent(db: Session, n: int = 10) -> list[dict]:
    """Return the N most recent alerts as dicts for the initial WS push."""
    rows = db.query(Alert).order_by(desc(Alert.created_at)).limit(n).all()
    result = []
    for a in rows:
        result.append({
            "id": str(a.id),
            "device_id": str(a.device_id) if a.device_id else None,
            "severity": a.severity.value if a.severity else "info",
            "source": a.source.value if a.source else "health_poll",
            "category": a.category,
            "message": a.message,
            "acknowledged": a.acknowledged,
            "acknowledged_by": a.acknowledged_by,
            "resolved": a.resolved,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "resolved_by": a.resolved_by,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "occurrence_count": a.occurrence_count,
            "root_cause_alert_id": str(a.root_cause_alert_id) if a.root_cause_alert_id else None,
            "suppressed": a.suppressed,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return result


async def _heartbeat_loop(websocket: WebSocket):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send_json({"type": "heartbeat"})
        except Exception:
            break


@router.websocket("/ws")
async def alerts_ws(websocket: WebSocket):
    await websocket.accept()

    # Initial snapshot of recent alerts
    db = SessionLocal()
    try:
        recent = _serialize_recent(db)
        await websocket.send_json({"type": "initial", "alerts": recent})
    finally:
        db.close()

    redis_client = event_bus.get_async_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(event_bus.ALERTS_CHANNEL)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None:
                continue
            # On any alert event, push refreshed recent alerts
            db = SessionLocal()
            try:
                recent = _serialize_recent(db)
                await websocket.send_json({"type": "update", "alerts": recent})
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pubsub.unsubscribe(event_bus.ALERTS_CHANNEL)
        await pubsub.close()
        await redis_client.close()