import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment, DeploymentStatus, HealthCheckResult
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User
from app.services import audit_service, event_bus, rollback_service
from app.tasks import retry_deployment_task, run_deployment_pipeline_task

router = APIRouter(prefix="/deployments", tags=["deployments"])

RETRYABLE_STATUSES = (DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK)
# A deployment that already rolled itself back (self-healing rollback,
# see pipeline_service) doesn't need a manual rollback on top -- only a
# straight FAILED deployment (validation/pre-flight/deploy failure, or a
# self-heal rollback attempt that itself failed) has a live config on the
# device that still needs undoing.
PARTIAL_ROLLBACK_STATUSES = (DeploymentStatus.FAILED,)


def _serialize(d: Deployment, db: Session) -> dict:
    checks = db.query(HealthCheckResult).filter(HealthCheckResult.deployment_id == d.id).all()
    # Batch context: how many total devices this deployment's change
    # request targeted, so the UI only needs to offer "partial rollback"
    # framing (vs. a plain rollback) when there's actually a batch to be
    # partial *about*. Cheap to compute -- one extra count query per row.
    import json as _json

    cr = db.get(ChangeRequest, d.change_request_id)
    target_device_count = 1
    if cr is not None and cr.additional_device_ids:
        try:
            target_device_count = 1 + len(_json.loads(cr.additional_device_ids))
        except (ValueError, TypeError):
            target_device_count = 1

    return {
        "id": str(d.id),
        "change_request_id": str(d.change_request_id),
        "device_id": str(d.device_id),
        "snapshot_id": str(d.snapshot_id) if d.snapshot_id else None,
        "protocol": d.protocol,
        "status": d.status.value,
        "error_message": d.error_message,
        "created_at": d.created_at,
        "target_device_count": target_device_count,
        "health_checks": [
            {
                "category": c.category,
                "check_name": c.check_name,
                "passed": c.passed == "true",
                "detail": c.detail,
                "checked_at": c.checked_at,
            }
            for c in checks
        ],
    }


@router.get("")
def list_deployments(
    change_request_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    q = db.query(Deployment).order_by(Deployment.created_at.desc())
    if change_request_id:
        q = q.filter(Deployment.change_request_id == change_request_id)
    if device_id:
        q = q.filter(Deployment.device_id == device_id)
    return [_serialize(d, db) for d in q.all()]


@router.get("/{deployment_id}")
def get_deployment(deployment_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.get(Deployment, deployment_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return _serialize(d, db)


@router.post("/{deployment_id}/retry")
def retry_deployment(deployment_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Re-runs the full snapshot -> deploy -> verify -> (rollback) pipeline
    for the device behind a FAILED or ROLLED_BACK deployment, without
    touching any other device on the same change request (see
    app.tasks.retry_deployment_task, which this dispatches).

    Only deployments in a terminal failure state are retryable -- a
    SUCCEEDED deployment has nothing to retry, and one still IN_PROGRESS
    would race the pipeline that's already running it.
    """
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status not in RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Deployment is '{deployment.status.value}' and can't be retried -- only "
                f"{', '.join(s.value for s in RETRYABLE_STATUSES)} deployments are retryable."
            ),
        )

    cr = db.get(ChangeRequest, deployment.change_request_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request for this deployment no longer exists")

    device = db.get(Device, deployment.device_id)
    if device and device.flagged_unstable:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{device.hostname}' is flagged unstable after repeated deployment failures. "
                "Clear the unstable flag (POST /devices/{id}/clear-unstable-flag) before retrying."
            ),
        )

    audit_service.record_event(
        db, actor=current_user.email, action="Deployment Retry Queued", result="Queued",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
        detail=f"Retrying deployment {deployment_id} (was {deployment.status.value}).",
    )

    task = retry_deployment_task.delay(str(cr.id), str(deployment.device_id), current_user.email)
    return {"message": "Retry queued.", "task_id": task.id, "change_request_id": str(cr.id), "device_id": str(deployment.device_id)}


@router.post("/{deployment_id}/rollback", response_model=dict, status_code=202)
def rollback_deployment(
    deployment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Partial rollback: undoes this ONE device's deployment on a
    multi-device change request, without touching any of the other
    devices the CR targeted -- whether those succeeded, are still in
    flight, or failed independently.

    Restores the exact pre-deploy snapshot that was captured for this
    device right before this deployment attempt (Deployment.snapshot_id),
    rather than requiring the caller to hunt it down in the device's full
    snapshot history the way a general-purpose rollback would. Runs
    through the standard rollback_service (own ChangeRequest, own
    Snapshot -> Deploy -> Health Monitor pipeline), so this device's undo
    gets exactly the same safety net as any other change and shows up
    independently in the audit trail / Change Requests / Deployments
    views -- it is a new, separate CR, not a mutation of the batch CR
    that failed.

    Only a FAILED deployment can be manually rolled back this way -- one
    that already self-healed (ROLLED_BACK) has nothing left to undo, and
    a SUCCEEDED device on the same batch should be rolled back explicitly
    from that device's own snapshot history (Devices page) if that's
    really the intent, not accidentally lumped in here.
    """
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status not in PARTIAL_ROLLBACK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Deployment is '{deployment.status.value}' -- only a FAILED deployment can be "
                "manually rolled back this way. A rolled-back deployment already restored itself; "
                "a succeeded one should be rolled back from the device's snapshot history instead."
            ),
        )

    if deployment.snapshot_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This deployment has no pre-deploy snapshot on file to restore (it likely failed "
                "before the snapshot step completed) -- use the device's snapshot history to pick "
                "a rollback target manually instead."
            ),
        )

    device = db.get(Device, deployment.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device for this deployment no longer exists")

    snapshot = db.get(ConfigSnapshot, deployment.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Pre-deploy snapshot for this deployment no longer exists")

    cr = db.get(ChangeRequest, deployment.change_request_id)

    try:
        rollback_cr = rollback_service.initiate_rollback(
            db, device, snapshot, current_user,
            reason=f"Partial rollback of deployment {deployment.id} (batch CR {deployment.change_request_id})",
        )
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    audit_service.record_event(
        db, actor=current_user.email, action="Partial Rollback Queued", result="Queued",
        device_hostname=device.hostname, change_request_id=rollback_cr.id,
        detail=(
            f"Rolling back only {device.hostname} from failed deployment {deployment.id} "
            f"on batch change request {deployment.change_request_id}"
            + (f" ('{cr.description}')" if cr else "")
            + "; other devices in that batch are unaffected."
        ),
    )

    run_deployment_pipeline_task.delay(str(rollback_cr.id), current_user.email)
    return {
        "message": f"Partial rollback queued for {device.hostname}.",
        "change_request_id": str(rollback_cr.id),
        "device_id": str(device.id),
    }


@router.get("/snapshots/{snapshot_id}/checksum")
def get_snapshot_checksum(snapshot_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Expose checksum/version only -- never the encrypted config contents."""
    snap = db.get(ConfigSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"id": str(snap.id), "checksum": snap.checksum, "version": snap.version, "created_at": snap.created_at}


@router.get("/{deployment_id}/logs")
def get_deployment_logs(deployment_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Returns the ordered timeline/logs of a deployment for real-time and historical views."""
    from app.models.deployment import DeploymentLog
    logs = db.query(DeploymentLog).filter(DeploymentLog.deployment_id == deployment_id).order_by(DeploymentLog.timestamp.asc()).all()
    return [
        {
            "id": str(lg.id),
            "step": lg.step,
            "level": lg.level,
            "message": lg.message,
            "timestamp": lg.timestamp
        }
        for lg in logs
    ]


@router.websocket("/ws")
async def deployments_ws(websocket: WebSocket):
    """
    Dedicated websocket for real-time deployment logs and status updates.
    """
    await websocket.accept()
    client = event_bus.get_async_client()
    ps = client.pubsub()
    await ps.subscribe("netguard:events")
    try:
        while True:
            message = await ps.get_message(ignore_subscribe_messages=True, timeout=None)
            if message:
                data = json.loads(message["data"])
                if data.get("event") in ("deployment_status_changed", "deployment_log"):
                    await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ps.unsubscribe()
        await ps.close()
        await client.close()
