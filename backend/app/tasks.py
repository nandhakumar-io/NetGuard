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
from app.models.config_drift import DriftBaseline
from app.models.device import Device
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


# --- Nightly drift detection (SRS: automated + on-demand drift scans) ---
#
# Same fan-out shape as the deployment pipeline above: one Celery task per
# device so a fleet of N devices scans in parallel across the worker pool
# instead of one long-blocking loop, and one device's protocol timeout
# can't delay every other device's scan. Scheduled nightly via Celery beat
# (see app.celery_app.conf.beat_schedule); also invokable directly for
# testing/backfills.


@celery_app.task(
    name="app.tasks.drift_detection_task",
    bind=True,
    # Infra retries only (DB hiccup, worker restart) -- protocol_manager
    # already retries the underlying SSH/NETCONF/RESTCONF read itself, and
    # a device that's simply unreachable shouldn't hold up the nightly
    # sweep for every other device.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def drift_detection_task(self, device_id: str, baseline: str = DriftBaseline.PREVIOUS_BACKUP.value) -> str:
    from app.services import drift_service

    db = SessionLocal()
    try:
        device = db.get(Device, uuid.UUID(device_id))
        if device is None:
            return "device_missing"
        try:
            result = drift_service.detect_drift(
                db, device, baseline=DriftBaseline(baseline), triggered_by="system:nightly-drift-scan"
            )
            return result.drift.severity.value
        except drift_service.NoBaselineError:
            # Nothing to compare against yet (no backup/golden config) --
            # not a failure, just nothing to do for this device tonight.
            return "no_baseline"
        except RuntimeError as exc:
            audit_service.record_event(
                db, actor="system:nightly-drift-scan", action="Drift Scan Failed", result="error",
                device_hostname=device.hostname, detail=str(exc),
            )
            db.commit()
            return "scan_failed"
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_nightly_drift_sweep_task")
def run_nightly_drift_sweep_task() -> int:
    """Celery beat entry point: fans out one drift_detection_task per
    device in inventory. Returns the number of devices scanned.
    """
    db = SessionLocal()
    try:
        device_ids = [str(d.id) for d in db.query(Device.id).all()]
    finally:
        db.close()

    for device_id in device_ids:
        drift_detection_task.delay(device_id)
    return len(device_ids)


# --- SNMP Monitoring / Health Dashboard (FR: SNMP polling) ---
#
# Same fan-out shape as the drift sweep above: one Celery task per device
# so an unreachable/slow device can't delay the rest of the fleet's poll,
# scheduled every settings.SNMP_POLL_INTERVAL_SECONDS via Celery beat.


@celery_app.task(
    name="app.tasks.snmp_poll_task",
    bind=True,
    # Infra retries only. snmp_service.poll_health already treats an
    # unreachable device as a normal (reachable=False) result rather than
    # raising, so a retry here only covers things like a DB hiccup --
    # not "the device didn't answer", which is expected fleet noise.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def snmp_poll_task(self, device_id: str) -> str:
    from app.services import metrics_service

    db = SessionLocal()
    try:
        device = db.get(Device, uuid.UUID(device_id))
        if device is None:
            return "device_missing"
        try:
            metric = metrics_service.poll_device(db, device)
            return metric.health_color.value if metric.health_color else "unknown"
        except metrics_service.SnmpNotConfiguredError:
            return "snmp_not_configured"
        except metrics_service.credential_service.CredentialNotFoundError as exc:
            audit_service.record_event(
                db, actor="system:snmp-poll", action="SNMP Poll Failed", result="error",
                device_hostname=device.hostname, detail=str(exc),
            )
            db.commit()
            return "credential_missing"
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_snmp_poll_sweep_task")
def run_snmp_poll_sweep_task() -> int:
    """Celery beat entry point: fans out one snmp_poll_task per
    SNMP-enabled device. Returns the number of devices polled.
    """
    db = SessionLocal()
    try:
        device_ids = [str(d.id) for d in db.query(Device.id).filter(Device.supports_snmp.is_(True)).all()]
    finally:
        db.close()

    for device_id in device_ids:
        snmp_poll_task.delay(device_id)
    return len(device_ids)