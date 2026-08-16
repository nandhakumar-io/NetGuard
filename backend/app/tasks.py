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
from datetime import datetime, timezone

from celery import chain, chord

from app.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.config_drift import DriftBaseline
from app.models.deployment import Deployment, DeploymentStatus
from app.models.device import Device
from app.services import (
    audit_service,
    event_bus,
    notification_service,
    pipeline_service,
)


def _seconds_since(past: datetime | None, now: datetime) -> float | None:
    """(now - past) in seconds, or None if `past` is None (never polled).
    Treats a naive `past` as UTC -- Device's poll-timestamp columns are
    DateTime(timezone=True) in Postgres, but some drivers (notably
    SQLite, used in tests) hand back naive datetimes regardless, and a
    naive/aware subtraction raises TypeError rather than just being
    wrong, so this normalizes instead of trusting the driver.
    """
    if past is None:
        return None
    if past.tzinfo is None:
        past = past.replace(tzinfo=timezone.utc)
    return (now - past).total_seconds()


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


@celery_app.task(
    name="app.tasks.retry_deployment_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def retry_deployment_task(self, cr_id: str, device_id: str, actor_email: str) -> str:
    """Dispatched by POST /deployments/{id}/retry. Runs the pipeline again
    for ONE device that previously failed/rolled back, without touching
    the other devices on the same change request. Unlike
    run_deployment_pipeline_task (which fans out every target device via
    a chord), this drives a single device end-to-end and re-aggregates
    the change request's overall status itself once done -- there's no
    second device to wait on.
    """
    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, uuid.UUID(cr_id))
        if cr is None:
            raise ValueError(f"ChangeRequest {cr_id} no longer exists")
        deployment = pipeline_service.run_deployment_for_device(db, cr, uuid.UUID(device_id), actor_email)
        pipeline_service.aggregate_change_request_status(db, cr)
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


@celery_app.task(name="app.tasks.canary_gate_task")
def canary_gate_task(canary_deployment_id: str, cr_id: str, remaining_device_ids: list[str], actor_email: str) -> str:
    """Chain callback that runs once the canary device's deploy_device_task
    has finished (SRS 6.6 canary multi-device deploy: 1 device -> health
    window -> rest of CR).

    - Canary SUCCEEDED: fan the remaining devices out as a normal chord
      (parallel, same as the non-canary path), so the bulk of the fleet
      still deploys concurrently -- only the canary is sequential.
    - Canary FAILED or ROLLED_BACK: the remaining devices are never
      touched. The change request is finalized off just the canary's
      result so the failure is visible immediately instead of waiting on
      devices that were intentionally never dispatched.
    """
    db = SessionLocal()
    try:
        canary_deployment = db.get(Deployment, uuid.UUID(canary_deployment_id))
        cr = db.get(ChangeRequest, uuid.UUID(cr_id))
        if cr is None or canary_deployment is None:
            return "change_request_or_canary_missing"

        if canary_deployment.status == DeploymentStatus.SUCCEEDED:
            audit_service.record_event(
                db, actor=actor_email, action="Canary Deploy Succeeded", result="Proceeding",
                change_request_id=cr.id,
                detail=f"Canary healthy -- deploying remaining {len(remaining_device_ids)} device(s) in parallel.",
            )
            if remaining_device_ids:
                header = [
                    deploy_device_task.s(cr_id, device_id, actor_email) for device_id in remaining_device_ids
                ]
                callback = finalize_change_request_task.s(cr_id, actor_email)
                chord(header)(callback)
                return "canary_passed_remaining_dispatched"
            # No other devices targeted -- canary was the whole rollout.
            pipeline_service.aggregate_change_request_status(db, cr)
            return "canary_passed_no_remaining"

        # Canary failed or was rolled back: abort the rest of the rollout.
        cr = pipeline_service.aggregate_change_request_status(db, cr)
        audit_service.record_event(
            db, actor=actor_email, action="Canary Deploy Failed", result=cr.status.value,
            change_request_id=cr.id,
            detail=(
                f"Canary device failed health checks ({canary_deployment.status.value}). "
                f"Remaining {len(remaining_device_ids)} device(s) were NOT deployed."
            ),
        )
        notification_service.notify(
            "Canary Deployment Aborted Rollout",
            f"Change request {str(cr.id)[:8]}: canary device failed "
            f"({canary_deployment.status.value}). Remaining {len(remaining_device_ids)} device(s) skipped.",
            severity="critical",
            change_request_id=cr.id, deployment_id=canary_deployment.id,
        )
        return "canary_failed_remaining_skipped"
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_deployment_pipeline_task")
def run_deployment_pipeline_task(cr_id: str, actor_email: str) -> None:
    """Entry point dispatched by POST /change-requests/{id}/approve. Fans
    out one deploy_device_task per target device (multi-device / parallel
    deployment, SRS 6.6) and schedules finalize_change_request_task to run
    once they've all finished, whatever the outcome of each.

    When `cr.canary_enabled` is set and the change request targets more
    than one device, the FIRST target device deploys alone; only once its
    health-monitoring window passes does the rest of the fleet fan out in
    parallel via canary_gate_task above. A failing canary skips the
    remaining devices entirely rather than deploying a config the canary
    just proved unsafe.
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
        canary_enabled = bool(cr.canary_enabled)
    finally:
        db.close()

    if canary_enabled and len(device_ids) > 1:
        canary_id, remaining_ids = device_ids[0], device_ids[1:]
        chain(
            deploy_device_task.s(cr_id, canary_id, actor_email),
            canary_gate_task.s(cr_id, remaining_ids, actor_email),
        ).apply_async()
        return

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
            # poll_device returns a dict (mirrors the old DeviceMetric row's
            # columns, see its docstring) since the VictoriaMetrics cutover --
            # not an ORM row, so this must be a key lookup, not attribute
            # access. health_color is already a plain string ("green" /
            # "yellow" / "red" / "unknown"), not an enum, so no `.value`.
            return metric.get("health_color") or "unknown"
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


@celery_app.task(
    name="app.tasks.reachability_task",
    bind=True,
    # Infra retries only -- reachability_service.ping_host already treats
    # an unreachable device as a normal (False) result, not an exception.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def reachability_task(self, device_id: str) -> str:
    from app.services import reachability_service

    db = SessionLocal()
    try:
        device = db.get(Device, uuid.UUID(device_id))
        if device is None:
            return "device_missing"
        status = reachability_service.check_device(db, device)
        device.last_reachability_poll_at = datetime.now(timezone.utc)
        db.commit()
        return status.value
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_reachability_sweep_task")
def run_reachability_sweep_task() -> int:
    """Celery beat entry point: pings every device that's actually due
    (SNMP-enabled or not -- unlike the SNMP poll sweep, reachability isn't
    conditional on SNMP being configured). Two things keep this sane past
    a handful of devices:

      - Per-device cadence: a device only gets enqueued once its own
        `reachability_poll_interval_seconds` override (or the fleet-wide
        REACHABILITY_POLL_INTERVAL_SECONDS default when unset) has
        actually elapsed since `last_reachability_poll_at`. Beat still
        ticks this sweep on its own fixed schedule, but a device with a
        looser override just gets skipped on the ticks it isn't due yet
        -- it doesn't get polled every tick regardless.
      - Jitter: due devices aren't all enqueued at once. Each gets a
        random `countdown` up to settings.REACHABILITY_POLL_JITTER_SECONDS
        so a fleet of hundreds doesn't fire hundreds of simultaneous ICMP
        probes in the same instant every sweep.

    Returns the number of devices actually enqueued (not the fleet size).
    """
    import random

    from app.core.config import settings

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due_ids: list[str] = []
        for device_id, interval_override, last_poll_at in db.query(
            Device.id, Device.reachability_poll_interval_seconds, Device.last_reachability_poll_at
        ).all():
            interval = interval_override or settings.REACHABILITY_POLL_INTERVAL_SECONDS
            elapsed = _seconds_since(last_poll_at, now)
            if elapsed is None or elapsed >= interval:
                due_ids.append(str(device_id))
    finally:
        db.close()

    jitter_max = max(0, settings.REACHABILITY_POLL_JITTER_SECONDS)
    for device_id in due_ids:
        countdown = random.uniform(0, jitter_max) if jitter_max else 0
        reachability_task.apply_async(args=[device_id], countdown=countdown)
    return len(due_ids)


@celery_app.task(name="app.tasks.run_snmp_poll_sweep_task")
def run_snmp_poll_sweep_task() -> int:
    """Celery beat entry point: fans out one snmp_poll_task per
    SNMP-enabled device that's actually due -- same per-device-cadence +
    jitter treatment as run_reachability_sweep_task above, using
    `snmp_poll_interval_seconds` / `last_snmp_poll_at` and
    settings.SNMP_POLL_JITTER_SECONDS instead. Returns the number of
    devices actually enqueued (not the fleet size).
    """
    import random

    from app.core.config import settings

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due_ids: list[str] = []
        for device_id, interval_override, last_poll_at in (
            db.query(Device.id, Device.snmp_poll_interval_seconds, Device.last_snmp_poll_at)
            .filter(Device.supports_snmp.is_(True))
            .all()
        ):
            interval = interval_override or settings.SNMP_POLL_INTERVAL_SECONDS
            elapsed = _seconds_since(last_poll_at, now)
            if elapsed is None or elapsed >= interval:
                due_ids.append(str(device_id))
    finally:
        db.close()

    jitter_max = max(0, settings.SNMP_POLL_JITTER_SECONDS)
    for device_id in due_ids:
        countdown = random.uniform(0, jitter_max) if jitter_max else 0
        snmp_poll_task.apply_async(args=[device_id], countdown=countdown)
    return len(due_ids)


# --- Compliance report scheduling (weekly / monthly email delivery) ---
#
# Same shape as every other scheduled task here: a thin Celery entry point
# that opens its own DB session and delegates the actual work to the
# service layer (app.services.compliance_report.deliver_scheduled_report),
# scheduled via Celery beat (see app.celery_app.conf.beat_schedule). The
# on-demand GET /reports/compliance endpoint is unaffected by this schedule.


@celery_app.task(
    name="app.tasks.run_weekly_compliance_report_task",
    bind=True,
    # Infra retries only (DB hiccup, worker restart) -- an SMTP failure is
    # already handled (logged + swallowed) inside notification_service, not
    # something retrying this task would fix.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_weekly_compliance_report_task(self) -> bool:
    from app.core.config import settings
    from app.services import compliance_report

    if not settings.COMPLIANCE_REPORT_WEEKLY_ENABLED:
        return False

    db = SessionLocal()
    try:
        return compliance_report.deliver_scheduled_report(
            db, window_days=settings.COMPLIANCE_REPORT_WEEKLY_WINDOW_DAYS, period_label="Weekly"
        )
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.run_monthly_compliance_report_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_monthly_compliance_report_task(self) -> bool:
    from app.core.config import settings
    from app.services import compliance_report

    if not settings.COMPLIANCE_REPORT_MONTHLY_ENABLED:
        return False

    db = SessionLocal()
    try:
        return compliance_report.deliver_scheduled_report(
            db, window_days=settings.COMPLIANCE_REPORT_MONTHLY_WINDOW_DAYS, period_label="Monthly"
        )
    finally:
        db.close()

@celery_app.task(
    name="app.tasks.run_weekly_change_request_digest_task",
    bind=True,
    # Same rationale as the compliance report tasks above: infra retries
    # only, an SMTP failure is already handled (logged + swallowed) inside
    # notification_service.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_weekly_change_request_digest_task(self) -> bool:
    """Celery beat entry point (see app.celery_app.conf.beat_schedule
    "weekly-change-request-digest"): emails a rollup of the week's change
    request activity -- volume, status breakdown, Critical Risk count,
    time-to-approve, top submitters, and anything still stuck in the
    pending queue -- to NOTIFY_EMAIL_RECIPIENTS via
    app.services.change_request_digest.deliver_scheduled_digest. No-ops
    (returns False without building/sending anything) if
    CHANGE_REQUEST_DIGEST_WEEKLY_ENABLED is off.
    """
    from app.core.config import settings
    from app.services import change_request_digest

    if not settings.CHANGE_REQUEST_DIGEST_WEEKLY_ENABLED:
        return False

    db = SessionLocal()
    try:
        return change_request_digest.deliver_scheduled_digest(
            db, window_days=settings.CHANGE_REQUEST_DIGEST_WEEKLY_WINDOW_DAYS
        )
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.run_firmware_upgrade_task",
    bind=True,
    # Same rationale as deploy_device_task: retries here cover infra
    # hiccups around the task itself, not the device conversation --
    # firmware_upgrade_service.run_upgrade already resolves a failed
    # in-flight upgrade to ROLLED_BACK/FAILED rather than leaving it
    # stuck, so a retried task just re-attempts a *new* job in that
    # terminal state's wake rather than double-upgrading a device.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_firmware_upgrade_task(self, job_id: str) -> str:
    from app.services import firmware_upgrade_service

    db = SessionLocal()
    try:
        job = firmware_upgrade_service.run_upgrade(db, uuid.UUID(job_id))
        return job.status.value
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.run_snapshot_retention_task",
    bind=True,
    # Infra retries only -- a DB hiccup mid-sweep is worth retrying;
    # there's no per-device external I/O here (this is pure DB
    # housekeeping) so nothing else about it is flaky.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_snapshot_retention_task(self) -> dict:
    """Celery beat entry point (see celery_app "snapshot-retention-sweep"):
    enforces the ConfigSnapshot retention policy fleet-wide every night.
    See app.services.snapshot_service.purge_expired_snapshots for what
    the policy actually is and why certain snapshots are always kept.
    """
    from app.services import snapshot_service

    db = SessionLocal()
    try:
        return snapshot_service.purge_expired_snapshots(db)
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_escalation_sweep_task")
def run_escalation_sweep_task() -> int:
    """Celery beat entry point (see celery_app "alert-escalation-sweep"):
    evaluates every enabled EscalationPolicy against currently active,
    unacknowledged alerts and notifies secondary contacts for any that
    have breached their policy's unack_minutes threshold. See
    app.services.escalation_service.run_escalation_sweep. Returns the
    number of escalations fired this tick.
    """
    from app.services import escalation_service

    db = SessionLocal()
    try:
        return escalation_service.run_escalation_sweep(db)
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_approval_sla_notify_sweep_task")
def run_approval_sla_notify_sweep_task() -> int:
    """Celery beat entry point (see celery_app "approval-sla-notify-sweep"):
    posts Slack/Teams reminders for PENDING_APPROVAL change requests that
    have just crossed into the "due soon" or "overdue" SLA stage, so the
    countdown that already exists in-app (GET
    /change-requests/pending-approvals) is visible wherever the approver
    actually works. See app.services.approval_sla_notifier_service.
    Returns the number of reminders posted this tick.
    """
    from app.services import approval_sla_notifier_service

    db = SessionLocal()
    try:
        return approval_sla_notifier_service.sweep_pending_approvals(db)
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_jit_expiry_notify_sweep_task")
def run_jit_expiry_notify_sweep_task() -> dict:
    """Celery beat entry point (see celery_app "jit-expiry-notify-sweep"):
    warns JIT Access holders whose grant is about to lapse
    (JIT_EXPIRY_WARNING_MINUTES out) and notifies when a grant actually
    expires, via the standard Slack/Teams/webhook/email/in-app fan-out --
    see app.services.jit_service.sweep_expiry_notifications. Returns
    {"warned": N, "expired": N} for this tick.
    """
    from app.services import jit_service

    db = SessionLocal()
    try:
        return jit_service.sweep_expiry_notifications(db)
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_recurring_window_generation_task")
def run_recurring_window_generation_task() -> int:
    """Celery beat entry point (see celery_app
    "recurring-maintenance-window-generation"): materializes upcoming
    concrete MaintenanceWindow rows for every enabled
    RecurringMaintenanceSchedule (patch Tuesdays, monthly firmware
    windows, ...), idempotently. Also run immediately whenever a
    schedule is created/updated (app.api.recurring_maintenance_schedules)
    so a new recurrence doesn't wait for the next daily tick. See
    app.services.recurring_window_service.generate_all. Returns the
    number of windows created this tick.
    """
    from app.services import recurring_window_service

    db = SessionLocal()
    try:
        return recurring_window_service.generate_all(db)
    finally:
        db.close()


# --- IPAM ------------------------------------------------------------


@celery_app.task(name="app.tasks.run_subnet_rescan_sweep_task")
def run_subnet_rescan_sweep_task() -> int:
    """Celery beat entry point (see celery_app "ipam-subnet-rescan-sweep"):
    re-runs scan_subnet() for every Subnet whose auto_rescan_enabled
    cadence (app.services.ipam_service.due_for_rescan) has elapsed, so
    scanned-host/utilization data doesn't go stale between manual clicks
    on the IPAM page -- same "beat ticks often, per-entity cadence
    decides who's due" shape as run_reachability_sweep_task above.
    Failures (e.g. nmap missing, subnet too large) are logged and skipped
    per-subnet so one bad subnet doesn't block the rest of the sweep.
    Returns the number of subnets actually rescanned.
    """
    import logging

    from app.services import ipam_service

    logger = logging.getLogger("netguard.tasks")
    db = SessionLocal()
    try:
        due = ipam_service.due_for_rescan(db)
        rescanned = 0
        for subnet in due:
            try:
                ipam_service.scan_subnet(db, subnet)
                rescanned += 1
            except Exception:
                logger.exception("Scheduled re-scan failed for subnet %s", subnet.cidr)
        return rescanned
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_ipam_conflict_alert_sweep_task")
def run_ipam_conflict_alert_sweep_task() -> int:
    """Celery beat entry point (see celery_app "ipam-conflict-alert-sweep"):
    turns app.services.ipam_service.fleet_conflicts from a pull-only
    check someone has to open the IPAM page to see into a real alert,
    same pattern as topology_service.raise_topology_change_alert. Raises
    one fleet-wide (device_id=None) WARNING alert per conflicting IP, with
    a category that includes the address so alert_service's existing
    active-alert-by-category dedup naturally suppresses re-raising on
    every subsequent tick while the conflict persists -- it only fires
    again once the conflict has cleared and reappears. Returns the number
    of conflicts alerted on this tick.
    """
    from app.models.alert import AlertSeverity, AlertSource
    from app.services import alert_service, ipam_service

    db = SessionLocal()
    try:
        conflicts = ipam_service.fleet_conflicts(db)
        for conflict in conflicts:
            hostnames = ", ".join(conflict["hostnames"])
            alert_service.raise_alert(
                db,
                device_id=None,
                severity=AlertSeverity.WARNING,
                source=AlertSource.HEALTH_POLL,
                category=f"IP Conflict: {conflict['ip_address']}",
                message=(
                    f"IP address {conflict['ip_address']} is claimed by more than one device: "
                    f"{hostnames}."
                ),
            )
        return len(conflicts)
    finally:
        db.close()


# --- GitOps sync ---------------------------------------------------------


@celery_app.task(
    name="app.tasks.git_repo_sync_task",
    bind=True,
    # Network I/O against an external Git host -- worth a couple of
    # retries on a transient clone/fetch failure, same rationale as the
    # SNMP/reachability tasks above. git_sync_service itself never raises
    # out of sync_repo (failures are recorded on the row), so this only
    # covers a crash in the task plumbing itself (e.g. DB hiccup).
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def git_repo_sync_task(self, repo_id: str) -> dict:
    """Entry point for both a manual sync (POST /gitops/repos/{id}/sync)
    and a webhook-triggered sync (POST /gitops/webhook/{id} on a push to
    the watched branch). Runs off the request thread since a clone/fetch
    against a real Git host can take longer than an HTTP client wants to
    wait.
    """
    from app.models.git_repo_config import GitRepoConfig
    from app.services import git_sync_service

    db = SessionLocal()
    try:
        repo = db.get(GitRepoConfig, uuid.UUID(repo_id))
        if repo is None:
            return {"error": "repo not found"}
        return git_sync_service.sync_repo(db, repo)
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_gitops_auto_sync_sweep_task")
def run_gitops_auto_sync_sweep_task() -> int:
    """Celery beat entry point: periodically re-pulls every
    auto_sync_enabled GitRepoConfig with a PULL/BIDIRECTIONAL direction,
    as a safety net for teams that haven't wired up a repo webhook (or
    whose webhook delivery failed) -- the same behavior a webhook trigger
    gets, just on a timer instead of on push. Returns how many repos were
    swept.
    """
    from app.models.git_repo_config import GitRepoConfig, GitSyncDirection
    from app.services import git_sync_service

    db = SessionLocal()
    try:
        repos = (
            db.query(GitRepoConfig)
            .filter(
                GitRepoConfig.auto_sync_enabled == True,  # noqa: E712
                GitRepoConfig.direction.in_([GitSyncDirection.PULL, GitSyncDirection.BIDIRECTIONAL]),
            )
            .all()
        )
        for repo in repos:
            git_sync_service.sync_repo(db, repo)
        return len(repos)
    finally:
        db.close()
