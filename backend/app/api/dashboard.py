import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models.device import Device, DeviceStatus
from app.models.deployment import Deployment, DeploymentStatus
from app.models.change_request import ChangeRequest

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Dashboard updates in under 2 seconds (NFR, section 8 Performance)
WEBSOCKET_PUSH_INTERVAL_SECONDS = 2


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

    return {
        "devices_online": devices_online,
        "devices_total": devices_total,
        "active_deployments": active_deployments,
        "failed_deployments": failed_deployments,
        "rollbacks": rollbacks,
        "pending_change_requests": pending_change_requests,
    }


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return _compute_summary(db)


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket):
    """Live Deployment Dashboard (SRS 6.9): pushes a fresh summary snapshot
    every couple of seconds so the frontend doesn't need to poll.
    """
    await websocket.accept()
    try:
        while True:
            db = SessionLocal()
            try:
                await websocket.send_json(_compute_summary(db))
            finally:
                db.close()
            await asyncio.sleep(WEBSOCKET_PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        pass
