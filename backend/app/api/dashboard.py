import asyncio
import contextlib

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.core.database import SessionLocal, get_db
from app.models.device import Device, DeviceStatus
from app.models.deployment import Deployment, DeploymentStatus
from app.models.change_request import ChangeRequest
from app.models.alert import Alert, AlertSeverity
from app.models.device_metric import DeviceMetric
from app.models.protocol_operation import ProtocolOperation
from app.models.snapshot import ConfigSnapshot
from app.models.config_drift import ConfigDrift, DriftStatus
from app.services import event_bus
from app.models.change_request import ChangeRequest, ChangeStatus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

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
        ChangeRequest.status.in_([ChangeStatus.PENDING_APPROVAL])
    ).count()

    # Alert counts for dashboard stat cards
    active_alerts = db.query(Alert).filter(Alert.resolved == False)  # noqa: E712
    critical_alerts = active_alerts.filter(Alert.severity == AlertSeverity.CRITICAL).count()
    warning_alerts = active_alerts.filter(Alert.severity == AlertSeverity.WARNING).count()

    # Open drift + circuit-breaker-flagged devices -- both tracked in the
    # DB already (config_drifts.status, devices.flagged_unstable) but
    # previously invisible anywhere on the dashboard.
    open_drifts = db.query(ConfigDrift).filter(ConfigDrift.status == DriftStatus.OPEN).count()
    flagged_unstable_devices_query = (
        db.query(Device).filter(Device.flagged_unstable == True).order_by(Device.unstable_since.desc())  # noqa: E712
    )
    flagged_unstable_count = flagged_unstable_devices_query.count()
    flagged_unstable_devices = [
        {
            "id": str(d.id),
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "unstable_since": d.unstable_since.isoformat() if d.unstable_since else None,
        }
        for d in flagged_unstable_devices_query.limit(5).all()
    ]

    # --- New Dashboard Widget Data ---
    
    # 1. Global Health Score & Top CPU/Memory
    # Get the single latest metric row for each device using a subquery
    latest_metrics_subq = db.query(
        DeviceMetric.device_id,
        func.max(DeviceMetric.polled_at).label("latest_polled_at")
    ).group_by(DeviceMetric.device_id).subquery()

    latest_metrics_query = db.query(DeviceMetric, Device.hostname, Device.ip_address)\
        .join(latest_metrics_subq, 
             (DeviceMetric.device_id == latest_metrics_subq.c.device_id) & 
             (DeviceMetric.polled_at == latest_metrics_subq.c.latest_polled_at))\
        .join(Device, Device.id == DeviceMetric.device_id)\
        .all()
    
    top_cpu = sorted(latest_metrics_query, key=lambda x: (x[0].cpu_utilization_pct or 0), reverse=True)[:5]
    top_memory = sorted(latest_metrics_query, key=lambda x: (x[0].memory_utilization_pct or 0), reverse=True)[:5]
    
    health_scores = [x[0].health_score for x in latest_metrics_query if x[0].health_score is not None]
    global_health_score = int(sum(health_scores) / len(health_scores)) if health_scores else 100

    top_cpu_devices = [{"hostname": r[1], "ip_address": r[2], "cpu": r[0].cpu_utilization_pct or 0} for r in top_cpu]
    top_memory_devices = [{"hostname": r[1], "ip_address": r[2], "memory": r[0].memory_utilization_pct or 0} for r in top_memory]

    # 2. Deployment Success Rate
    deployments_successful = db.query(Deployment).filter(Deployment.status == DeploymentStatus.SUCCEEDED).count()
    deployments_total_finished = db.query(Deployment).filter(Deployment.status.in_([
        DeploymentStatus.SUCCEEDED, 
        DeploymentStatus.FAILED, 
        DeploymentStatus.ROLLED_BACK
    ])).count()
    
    success_rate = round((deployments_successful / deployments_total_finished * 100), 1) if deployments_total_finished > 0 else 100.0

    # 3. Recent Backups (Snapshots)
    recent_backups_query = db.query(ConfigSnapshot, Device.hostname)\
        .join(Device, Device.id == ConfigSnapshot.device_id)\
        .order_by(desc(ConfigSnapshot.created_at))\
        .limit(5).all()
        
    recent_backups = [{
        "id": str(r[0].id), 
        "version": r[0].version, 
        "created_at": r[0].created_at.isoformat() if r[0].created_at else "", 
        "hostname": r[1]
    } for r in recent_backups_query]

    # 4. Recent Protocol Operations
    recent_ops_query = db.query(ProtocolOperation, Device.hostname)\
        .outerjoin(Device, Device.id == ProtocolOperation.device_id)\
        .order_by(desc(ProtocolOperation.created_at))\
        .limit(5).all()
        
    recent_protocol_operations = [{
        "id": str(r[0].id),
        "protocol": r[0].protocol.value if hasattr(r[0].protocol, "value") else r[0].protocol,
        "operation": r[0].operation,
        "success": r[0].success,
        "created_at": r[0].created_at.isoformat() if r[0].created_at else "",
        "operator": r[0].operator,
        "device_hostname": r[1] or "Unknown"
    } for r in recent_ops_query]

    return {
        "devices_online": devices_online,
        "devices_total": devices_total,
        "active_deployments": active_deployments,
        "failed_deployments": failed_deployments,
        "rollbacks": rollbacks,
        "pending_change_requests": pending_change_requests,
        "critical_alerts": critical_alerts,
        "warning_alerts": warning_alerts,
        "open_drifts": open_drifts,
        "flagged_unstable_count": flagged_unstable_count,
        "flagged_unstable_devices": flagged_unstable_devices,
        
        "global_health_score": global_health_score,
        "deployment_success_rate": success_rate,
        "top_cpu_devices": top_cpu_devices,
        "top_memory_devices": top_memory_devices,
        "recent_backups": recent_backups,
        "recent_protocol_operations": recent_protocol_operations,
    }


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return _compute_summary(db)


async def _heartbeat_loop(websocket: WebSocket):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            await websocket.send_json(_compute_summary(db))
        finally:
            db.close()


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket):
    await websocket.accept()

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