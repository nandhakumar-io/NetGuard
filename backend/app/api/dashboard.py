import asyncio
import contextlib

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models.device import Device, DeviceStatus
from app.models.deployment import Deployment, DeploymentStatus
from app.models.change_request import ChangeRequest
from app.models.config_drift import ConfigDrift
from app.services import event_bus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Fallback heartbeat only -- keeps idle connections alive / lets clients
# detect a dead socket. Real updates are pushed the instant an event
# fires (see event_bus.publish_event), not on this interval.
HEARTBEAT_INTERVAL_SECONDS = 30


def _compute_summary(db: Session) -> dict:
    devices_online = db.query(Device).filter(Device.status == DeviceStatus.ONLINE).count()
    devices_total = db.query(Device).count()
    active_deployments = db.query(Deployment).filter(
        Deployment.status.in_([DeploymentStatus.QUEUED, DeploymentStatus.IN_PROGRESS])
    ).count()
    failed_deployments = db.query(Deployment).filter(Deployment.status == DeploymentStatus.FAILED).count()
    rollbacks = db.query(Deployment).filter(Deployment.status == DeploymentStatus.ROLLED_BACK).count()
    pending_change_requests = db.query(ChangeRequest).filter(
        ChangeRequest.status.in_(["pending_approval"])
    ).count()
    devices_with_unresolved_drift = (
        db.query(ConfigDrift.device_id)
        .filter(ConfigDrift.drifted == "true", ConfigDrift.resolved == "false")
        .distinct()
        .count()
    )

    return {
        "devices_online": devices_online,
        "devices_total": devices_total,
        "active_deployments": active_deployments,
        "failed_deployments": failed_deployments,
        "rollbacks": rollbacks,
        "pending_change_requests": pending_change_requests,
        "devices_with_unresolved_drift": devices_with_unresolved_drift,
    }


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return _compute_summary(db)


async def _heartbeat_loop(websocket: WebSocket):
    """Sends a fresh summary on a slow interval purely as a keepalive /
    safety net (e.g. a client that connected between two events, or a
    dropped pub/sub message). This is NOT the update mechanism -- it's a
    much slower backstop than the old 2s poll loop.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            await websocket.send_json(_compute_summary(db))
        finally:
            db.close()


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket):
    """Live Deployment Dashboard (SRS 6.9): pushes a fresh summary snapshot
    the moment a relevant event happens (device status change, deployment
    status change, change request submitted/approved/etc) instead of
    polling the DB on a fixed interval regardless of activity. Events are
    published by pipeline_service/tasks over Redis pub/sub so this works
    across processes (FastAPI workers + Celery workers).
    """
    await websocket.accept()

    # Send an initial snapshot immediately so the UI isn't blank while
    # waiting for the first event.
    db = SessionLocal()
    try:
        await websocket.send_json(_compute_summary(db))
    finally:
        db.close()

    redis_client = event_bus.get_async_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(event_bus.DASHBOARD_CHANNEL)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None:
                continue
            db = SessionLocal()
            try:
                await websocket.send_json(_compute_summary(db))
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pubsub.unsubscribe(event_bus.DASHBOARD_CHANNEL)
        await pubsub.close()
        await redis_client.close()
