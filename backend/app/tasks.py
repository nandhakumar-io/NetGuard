"""Celery tasks for the deployment pipeline.

Two tasks, chained with a Celery `chord` so multiple devices on one change
request deploy concurrently (SRS 6.6) instead of one-at-a-time:

    run_deployment_pipeline_task(cr_id)
        -> chord(
               [deploy_device_task(cr_id, device_id) for device_id in targets],
               finalize_change_request_task(cr_id),
           )

`deploy_device_task` is the unit Celery actually parallelizes: each device
gets its own task, its own DB session, and its own worker slot, so N
devices on a change request run in parallel across the worker pool rather
than sequentially in one long-blocking loop (the old behavior in
pipeline_service.run_deployment_pipeline). Once every device's task has
finished, the chord callback rolls the per-device results up into a single
ChangeRequest.status.
"""
import uuid

from celery import chord

from app.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.change_request import ChangeRequest, ChangeStatus
from app.services import audit_service, event_bus, pipeline_service


@celery_app.task(
    name="app.tasks.deploy_device_task",
    bind=True,
    # Retries here cover *infrastructure* failures (DB hiccup, worker OOM,
    # etc) around the task itself. Retries of the actual SSH/Netmiko
    # connection to the device are already handled inside
    # deployment_engine.deploy_config/rollback_config with their own
    # backoff -- we don't want to double up and retry a full
    # snapshot->deploy->health-check->rollback cycle just because one SSH
    # attempt inside it was retried.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def deploy_device_task(self, cr_id: str, device_id: str, actor_email: str) -> str:
    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, uuid.UUID(cr_id))
        if cr is None:
            raise ValueError(f"ChangeRequest {cr_id} no longer exists")
        deployment = pipeline_service.run_deployment_for_device(db, cr, uuid.UUID(device_id), actor_email)
        return str(deployment.id)
    finally:
        db.close()


@celery_app.task(name="app.tasks.finalize_change_request_task")
def finalize_change_request_task(_deployment_ids: list[str], cr_id: str, actor_email: str) -> str:
    """Chord callback: runs once every deploy_device_task for this change
    request has completed (successfully or not). Rolls the per-device
    Deployment outcomes up into cr.status and records the final audit event.
    """
    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, uuid.UUID(cr_id))
        if cr is None:
            return "change_request_missing"

        cr = pipeline_service.aggregate_change_request_status(db, cr)
        audit_service.record_event(
            db, actor=actor_email, action="Pipeline Completed", result=cr.status.value,
            change_request_id=cr.id,
            detail=f"{len(pipeline_service.target_device_ids(cr))} device(s) targeted",
        )
        return cr.status.value
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_deployment_pipeline_task")
def run_deployment_pipeline_task(cr_id: str, actor_email: str) -> None:
    """Entry point dispatched by POST /change-requests/{id}/approve. Fans
    out one deploy_device_task per target device (multi-device / parallel
    deployment, SRS 6.6) and schedules finalize_change_request_task to run
    once they've all finished, whatever the outcome of each.
    """
    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, uuid.UUID(cr_id))
        if cr is None:
            return
        cr.status = ChangeStatus.DEPLOYING
        db.commit()
        event_bus.publish_event(
            "change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id)
        )
        device_ids = [str(d) for d in pipeline_service.target_device_ids(cr)]
    finally:
        db.close()

    header = [deploy_device_task.s(cr_id, device_id, actor_email) for device_id in device_ids]
    callback = finalize_change_request_task.s(cr_id, actor_email)
    chord(header)(callback)
