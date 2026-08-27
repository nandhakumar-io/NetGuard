import json
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user, get_current_user_ws, get_tenant_scope
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment, DeploymentStatus, HealthCheckResult
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User
from app.schemas.rollback import DeploymentRollbackPreviewResponse
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


def _get_scoped_deployment(db: Session, deployment_id: uuid.UUID, tenant_id) -> Deployment:
    """Fetches a Deployment and enforces tenant ownership via its target
    Device -- Deployment has no tenant_id column of its own (it's a
    consequence of a ChangeRequest against a Device), so scoping rides on
    Device.tenant_id, same convention as app.api.change_requests."""
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if tenant_id is not None:
        device = db.get(Device, deployment.device_id)
        if device is not None and device.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


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
    tenant_id=Depends(get_tenant_scope),
):
    q = db.query(Deployment).order_by(Deployment.created_at.desc())
    if tenant_id is not None:
        q = q.join(Device, Deployment.device_id == Device.id).filter(Device.tenant_id == tenant_id)
    if change_request_id:
        q = q.filter(Deployment.change_request_id == change_request_id)
    if device_id:
        q = q.filter(Deployment.device_id == device_id)
    return [_serialize(d, db) for d in q.all()]


@router.get("/{deployment_id}")
def get_deployment(deployment_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user), tenant_id=Depends(get_tenant_scope)):
    d = _get_scoped_deployment(db, deployment_id, tenant_id)
    return _serialize(d, db)


@router.post("/{deployment_id}/retry")
def retry_deployment(deployment_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user), tenant_id=Depends(get_tenant_scope)):
    """Re-runs the full snapshot -> deploy -> verify -> (rollback) pipeline
    for the device behind a FAILED or ROLLED_BACK deployment, without
    touching any other device on the same change request (see
    app.tasks.retry_deployment_task, which this dispatches).

    Only deployments in a terminal failure state are retryable -- a
    SUCCEEDED deployment has nothing to retry, and one still IN_PROGRESS
    would race the pipeline that's already running it.
    """
    deployment = _get_scoped_deployment(db, deployment_id, tenant_id)

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
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Deployment Retry Queued", result="Queued",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
        detail=f"Retrying deployment {deployment_id} (was {deployment.status.value}).",
    )

    task = retry_deployment_task.delay(str(cr.id), str(deployment.device_id), current_user.email)
    return {"message": "Retry queued.", "task_id": task.id, "change_request_id": str(cr.id), "device_id": str(deployment.device_id)}


def _validate_rollback_target(db: Session, deployment_id: uuid.UUID, tenant_id) -> tuple[Deployment, Device, ConfigSnapshot]:
    """Shared validation for both the dry-run preview and the actual
    partial rollback below -- keeps the two endpoints from drifting on
    what makes a deployment eligible (same checks a preview shows would
    block must be the same checks the real POST enforces, or the preview
    is just decoration).
    """
    deployment = _get_scoped_deployment(db, deployment_id, tenant_id)

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

    return deployment, device, snapshot


@router.get("/{deployment_id}/rollback/preview", response_model=DeploymentRollbackPreviewResponse)
def preview_deployment_rollback(
    deployment_id: uuid.UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Dry-run counterpart to POST /deployments/{id}/rollback: shows the
    diff a partial rollback would apply -- what's live on this one device
    right now vs. the pre-deploy snapshot it would be restored to --
    without creating a ChangeRequest or pushing anything. Same eligibility
    checks as the real rollback (FAILED deployment, snapshot on file), so
    "can I roll back?" and "what would it change?" never disagree.
    Powers the confirmation step on the Deployments page and the
    `rollback <deployment-id>` ChatOps command.
    """
    deployment, device, snapshot = _validate_rollback_target(db, deployment_id, tenant_id)

    try:
        preview = rollback_service.preview_rollback(db, device, snapshot)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return DeploymentRollbackPreviewResponse(
        deployment_id=deployment.id,
        device_id=device.id,
        hostname=device.hostname,
        target_version=preview["target_version"],
        current_source=preview["current_source"],
        diff=preview["diff"],
        identical=preview["identical"],
        added_lines=preview["added_lines"],
        removed_lines=preview["removed_lines"],
        warning=preview["warning"],
        blocked=preview["blocked"],
        blocked_reason=preview["blocked_reason"],
    )


@router.post("/{deployment_id}/rollback", response_model=dict, status_code=202)
def rollback_deployment(
    deployment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
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

    Pair with GET /{deployment_id}/rollback/preview to show the diff
    before calling this -- that dry run uses the exact same validation as
    here, so anything it doesn't block, this won't either.
    """
    deployment, device, snapshot = _validate_rollback_target(db, deployment_id, tenant_id)
    cr = db.get(ChangeRequest, deployment.change_request_id)

    try:
        rollback_cr = rollback_service.initiate_rollback(
            db, device, snapshot, current_user,
            reason=f"Partial rollback of deployment {deployment.id} (batch CR {deployment.change_request_id})",
        )
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Partial Rollback Queued", result="Queued",
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
def get_snapshot_checksum(snapshot_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user), tenant_id=Depends(get_tenant_scope)):
    """Expose checksum/version only -- never the encrypted config contents."""
    snap = db.get(ConfigSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    if tenant_id is not None:
        device = db.get(Device, snap.device_id)
        if device is not None and device.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"id": str(snap.id), "checksum": snap.checksum, "version": snap.version, "created_at": snap.created_at}


@router.get("/{deployment_id}/logs")
def get_deployment_logs(deployment_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user), tenant_id=Depends(get_tenant_scope)):
    """Returns the ordered timeline/logs of a deployment for real-time and historical views."""
    from app.models.deployment import DeploymentLog
    _get_scoped_deployment(db, deployment_id, tenant_id)  # 404s if out of tenant scope
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


def _event_in_tenant(data: dict, tenant_id: uuid.UUID) -> bool:
    """True if a `deployment_status_changed`/`deployment_log` event
    (see app.services.pipeline_service / _log_deployment) belongs to the
    caller's tenant. Both event types carry `deployment_id` -- resolved
    the same way _get_scoped_deployment does for the REST endpoints
    (Deployment has no tenant_id of its own; it rides on its target
    Device's), via a short-lived session since the websocket loop has no
    long-running one of its own. Fails closed: an event this can't
    positively place in the caller's tenant (missing/unresolvable
    deployment_id -- e.g. a legacy event published before deployment_id
    was added, or the deployment/device having since been deleted) is
    dropped rather than delivered, since a false negative here just
    costs a UI refresh the next poll picks up, while a false positive
    leaks another tenant's deployment activity.
    """
    deployment_id = data.get("deployment_id")
    if not deployment_id:
        return False
    db = SessionLocal()
    try:
        deployment = db.get(Deployment, uuid.UUID(deployment_id))
        if deployment is None:
            return False
        device = db.get(Device, deployment.device_id)
        return device is not None and device.tenant_id == tenant_id
    except (ValueError, TypeError):
        return False
    finally:
        db.close()


@router.websocket("/ws")
async def deployments_ws(websocket: WebSocket, token: str = Query("")):
    """
    Dedicated websocket for real-time deployment logs and status updates.
    """
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

    # None for MSP staff (unfiltered -- every tenant), same convention as
    # every REST endpoint in this router -- see app.core.deps.
    # get_current_tenant_id. Resolved once up front rather than re-run
    # per message since it only depends on the user, not the event.
    tenant_id = None if user.is_msp_staff else user.tenant_id

    await websocket.accept()
    client = event_bus.get_async_client()
    ps = client.pubsub()
    # Was subscribing to the literal string "netguard:events", which
    # doesn't match this JetStream stream's subject space
    # ("netguard.>", see event_bus.STREAM_SUBJECTS) -- and even had it
    # matched, was reading `data["event"]` when publish_event() writes
    # `data["type"]`. Net effect: this socket never actually delivered a
    # single real update; it just idled until the client disconnected.
    await ps.subscribe(event_bus.DASHBOARD_CHANNEL)
    try:
        while True:
            message = await ps.get_message(ignore_subscribe_messages=True, timeout=None)
            if message:
                data = json.loads(message["data"])
                if data.get("type") not in ("deployment_status_changed", "deployment_log"):
                    continue
                if tenant_id is not None and not _event_in_tenant(data, tenant_id):
                    continue
                await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ps.unsubscribe()
        await ps.close()
        await client.close()
