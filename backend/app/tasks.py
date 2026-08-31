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
import logging
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

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.dispatch_alert_notification_task",
    bind=True,
    # Notifications must reach Slack/Teams/push/webhooks as close to
    # "the moment the alert fired" as the queue can manage -- previously
    # alert_service._dispatch_notification called notification_service.notify()
    # in-line, inside the same synchronous call stack as the SNMP poll
    # that raised the alert. notify() makes ~5-10 sequential blocking
    # HTTP calls (Slack, Teams, SMTP, Telegram, every enabled webhook,
    # every push subscription, every syslog destination), each with its
    # own multi-second timeout -- one slow/unreachable endpoint serially
    # delayed everything behind it *and* blocked the poll task from
    # moving on to the next device, so "immediately after the alert"
    # could actually be tens of seconds later, worse under load.
    # Enqueuing this as its own task fires it the instant the alert is
    # committed, off the polling/request path entirely, and retries
    # transient failures (a Redis hiccup fetching subscriptions, a DB
    # blip persisting delivery-attempt rows) instead of silently
    # swallowing them the way the inline call's bare try/except did.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    acks_late=True,
)
def dispatch_alert_notification_task(
    self,
    *,
    event: str,
    message: str,
    severity: str,
    device_hostname: str | None,
    tenant_id: str | None,
    alert_id: str | None,
) -> str:
    notification_service.notify(
        event=event,
        message=message,
        severity=severity,
        device_hostname=device_hostname,
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        alert_id=alert_id,
    )
    return "dispatched"


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
    from app.core.config import settings
    from app.services import device_job_service, metrics_service
    from app.services.device_job_service import (
        DeviceJobFailedError,
        DeviceJobTimeoutError,
        DeviceOperation,
    )

    db = SessionLocal()
    try:
        device = db.get(Device, uuid.UUID(device_id))
        if device is None:
            return "device_missing"
        try:
            # Section 4 key-rescoping follow-up: this worker (`poller`)
            # no longer holds DEVICE_CREDENTIAL_ENCRYPTION_KEY, so SNMP
            # community/v3 credentials can't be decrypted here anymore --
            # dispatch to the Device Gateway instead, same pattern as
            # every other device-facing operation.
            if settings.DEVICE_GATEWAY_ENABLED:
                job_result = device_job_service.submit_job_sync(
                    tenant_id=str(device.tenant_id),
                    device_id=str(device.id),
                    operation=DeviceOperation.SNMP_POLL,
                    params={},
                    requested_by="system:snmp-poll",
                )
                import json
                metric = json.loads(job_result.output) if job_result.output else {}
            else:
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
        except (DeviceJobTimeoutError, DeviceJobFailedError) as exc:
            audit_service.record_event(
                db, actor="system:snmp-poll", action="SNMP Poll Failed", result="error",
                device_hostname=device.hostname, detail=str(exc),
            )
            db.commit()
            return "gateway_job_failed"
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

        # Custom alert rules keyed on ping_packet_loss_pct live on the
        # SNMP-poll-driven evaluator (alert_rule_engine), but packet loss
        # is inherently a ping-path measurement, not something the SNMP
        # poll has any way to compute -- so this sweep is the one that
        # feeds it. Only bothers with the extra 5-probe burst when a rule
        # actually asks for this metric (checked once, cheaply) --
        # fleets that don't use it pay zero extra ping traffic.
        from app.models.alert_rule import AlertRule, AlertRuleMetric

        wants_loss_metric = (
            db.query(AlertRule.id)
            .filter(AlertRule.enabled == True, AlertRule.metric == AlertRuleMetric.PING_PACKET_LOSS_PCT)
            .first()
            is not None
        )
        if wants_loss_metric:
            from app.services import alert_rule_engine
            from app.services.snmp_service import SnmpMetrics

            loss_pct = reachability_service.measure_packet_loss_pct(device.ip_address)
            if loss_pct is not None:
                synthetic_metrics = SnmpMetrics(reachable=True, ping_packet_loss_pct=loss_pct)
                alert_rule_engine.evaluate_rules(db, device, synthetic_metrics)
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


@celery_app.task(name="app.tasks.standalone_ap_poll_task")
def standalone_ap_poll_task(ap_id: str) -> dict:
    """Per-AP unit of run_standalone_ap_poll_sweep_task below. Own DB
    session per task, same shape as reachability_task/snmp_poll_task."""
    from app.models.wireless import WirelessAP
    from app.services import wireless_service

    db = SessionLocal()
    try:
        ap = db.get(WirelessAP, uuid.UUID(ap_id))
        if ap is None or ap.source != "manual":
            return {"error": "not_found_or_not_manual"}
        result = wireless_service.poll_standalone_ap(db, ap)
        ap.polled_at = datetime.now(timezone.utc)
        db.commit()
        return result
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_standalone_ap_poll_sweep_task")
def run_standalone_ap_poll_sweep_task() -> int:
    """Celery beat entry point: keeps manually-added (source="manual")
    wireless APs' client_count/SSID data current on their own cadence.

    Before this task existed, standalone APs (Ruckus/TP-Link/MikroTik
    added by hand -- see WirelessAP.source docstring) only ever got
    re-polled when someone hit POST /aps/{id}/check from the UI, so
    client_count sat frozen (often at 0, whatever was typed in at
    creation) for anyone who didn't know to click that. Controller-
    managed APs never had this problem because poll_wireless_controller
    already runs on the snmp-poll-sweep beat entry -- this is the same
    treatment for the APs that sweep doesn't cover.

    Same due/jitter shape as run_reachability_sweep_task: only APs whose
    STANDALONE_AP_POLL_INTERVAL_SECONDS has actually elapsed since
    polled_at get enqueued, staggered with jitter so a large fleet of
    manual APs doesn't fire simultaneous SNMP GETs every tick.
    """
    import random

    from app.core.config import settings
    from app.models.wireless import WirelessAP

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due_ids: list[str] = []
        for ap_id, polled_at in db.query(WirelessAP.id, WirelessAP.polled_at).filter(
            WirelessAP.source == "manual"
        ).all():
            elapsed = _seconds_since(polled_at, now)
            if elapsed is None or elapsed >= settings.STANDALONE_AP_POLL_INTERVAL_SECONDS:
                due_ids.append(str(ap_id))
    finally:
        db.close()

    jitter_max = max(0, settings.STANDALONE_AP_POLL_JITTER_SECONDS)
    for ap_id in due_ids:
        countdown = random.uniform(0, jitter_max) if jitter_max else 0
        standalone_ap_poll_task.apply_async(args=[ap_id], countdown=countdown)
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
    name="app.tasks.run_tenant_digest_dispatch_task",
    bind=True,
    # Infra retries only, same rationale as the other scheduled-report
    # tasks: an individual subscription's SMTP failure is already logged
    # + swallowed inside tenant_digest_service.run_due_digests, so a
    # retry here is only for a DB/broker hiccup around the sweep itself.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_tenant_digest_dispatch_task(self) -> int:
    """Celery beat entry point (see app.celery_app.conf.beat_schedule
    "tenant-digest-dispatch"), run hourly. Finds every active
    TenantDigestSubscription whose cadence/hour(/day) matches the current
    UTC hour and delivers each one -- see
    app.services.tenant_digest_service.run_due_digests. Returns the
    number of subscriptions processed this run (sent or skipped, not
    just sent).
    """
    from app.services import tenant_digest_service

    db = SessionLocal()
    try:
        return tenant_digest_service.run_due_digests(db)
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


@celery_app.task(name="app.tasks.run_initial_subnet_scan_task")
def run_initial_subnet_scan_task(subnet_id: str) -> bool:
    """Fired once, right after a subnet is created (see app.api.ipam.create_subnet),
    so a freshly-added subnet gets its live nmap ping-sweep immediately instead of
    sitting with "used" only reflecting devices NetGuard already manages until
    someone remembers to click Scan or the next auto_rescan_enabled cadence fires.
    Runs off the request thread since scan_subnet's nmap sweep can take a while on
    larger CIDRs. Best-effort: a failure here (nmap missing, subnet unreachable)
    just means the subnet falls back to the manual Scan button / periodic sweep,
    so it's logged and swallowed rather than surfaced anywhere.
    """
    import logging
    import uuid as _uuid

    from app.models import Subnet
    from app.services import ipam_service

    logger = logging.getLogger("netguard.tasks")
    db = SessionLocal()
    try:
        subnet = db.get(Subnet, _uuid.UUID(subnet_id))
        if not subnet:
            return False
        try:
            ipam_service.scan_subnet(db, subnet)
            return True
        except Exception:
            logger.exception("Initial scan failed for newly-created subnet %s", subnet.cidr)
            return False
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


@celery_app.task(name="app.tasks.run_flow_alert_sweep_task")
def run_flow_alert_sweep_task() -> int:
    """Celery beat entry point (see celery_app "flow-alert-sweep"): runs
    app.services.flow_service.evaluate_flow_alert_rules against the
    latest rolling window of FlowRecord data, turning Traffic Analysis'
    Top Talkers from a pull-only page into a real alert -- same
    "close the loop from report to alert" pattern as
    run_ipam_conflict_alert_sweep_task above. Returns the number of new
    alerts raised this tick.
    """
    from app.services import flow_service

    db = SessionLocal()
    try:
        return flow_service.evaluate_flow_alert_rules(db)
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


@celery_app.task(
    name="app.tasks.run_network_discovery_scan_task",
    bind=True,
    # A crash in the task plumbing itself (DB hiccup) is worth one retry;
    # network_discovery_service.run_scan already treats per-host probe
    # failures as normal (logged, skipped) rather than exceptions, so a
    # retry here re-runs the whole sweep, not just a failed host.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_network_discovery_scan_task(self, scan_id: str, community: str | None = None) -> str:
    """Entry point for POST /discovery/scans -- runs off the request
    thread since sweeping up to 1024 hosts (see
    network_discovery_service.MAX_SCAN_HOSTS) can take tens of seconds,
    longer than an HTTP client should wait synchronously.
    """
    from app.models.network_discovery import DiscoveryScan, DiscoveryScanStatus
    from app.services import network_discovery_service

    db = SessionLocal()
    try:
        scan = db.get(DiscoveryScan, uuid.UUID(scan_id))
        if scan is None:
            return "scan_missing"
        if scan.status == DiscoveryScanStatus.CANCELLED:
            return "cancelled"

        scan.status = DiscoveryScanStatus.RUNNING
        scan.celery_task_id = self.request.id
        db.add(scan)
        db.commit()

        try:
            network_discovery_service.run_scan(db, scan, community)
            scan.status = DiscoveryScanStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 -- bad CIDR slipped past API validation, etc.
            scan.status = DiscoveryScanStatus.FAILED
            scan.error = str(exc)[:500]
            logger.warning("Network discovery scan %s failed", scan_id, exc_info=True)
        finally:
            scan.completed_at = datetime.now(timezone.utc)
            db.add(scan)
            db.commit()
        return scan.status.value
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_bulk_topology_discovery_task")
def run_bulk_topology_discovery_task() -> dict:
    """Entry point for POST /devices/discover-topology-all (the Topology
    page's "Run discovery on all devices" button). Runs the same LLDP/CDP
    SNMP discovery + neighbor-persist as GET /devices/{id}/discovery, but
    across every SNMP-configured device in the fleet in one shot, so
    real confirmed links (topology_service.build_topology's LLDP/CDP edge
    source) get populated without clicking into each device's Discovery
    tab individually. Runs off the request thread since a full-fleet
    sweep can take a while. Best-effort per device: one device's SNMP
    timeout/credential problem is logged and skipped, not fatal to the
    rest of the sweep.
    """
    from app.api.devices import _persist_discovered_neighbors
    from app.core.config import settings
    from app.services import credential_service, metrics_service, snmp_service

    db = SessionLocal()
    try:
        devices = (
            db.query(Device)
            .filter(Device.supports_snmp.is_(True), Device.snmp_version.isnot(None))
            .all()
        )
        succeeded = 0
        failed = 0
        skipped_no_creds = 0
        for device in devices:
            try:
                auth = metrics_service.build_snmp_auth(device)
            except credential_service.CredentialNotFoundError:
                skipped_no_creds += 1
                continue
            try:
                result = snmp_service.discover_inventory(
                    device.ip_address,
                    auth,
                    timeout=settings.SNMP_TIMEOUT_SECONDS,
                    vendor=device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
                )
                _persist_discovered_neighbors(db, device.id, result)
                db.commit()
                succeeded += 1
            except Exception:
                db.rollback()
                failed += 1
                logger.exception("Bulk topology discovery failed for device %s", device.hostname)
        return {
            "total_snmp_devices": len(devices),
            "succeeded": succeeded,
            "failed": failed,
            "skipped_no_credentials": skipped_no_creds,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_discovery_schedule_sweep_task")
def run_discovery_schedule_sweep_task() -> dict:
    """Celery beat entry point (see celery_app "discovery-schedule-sweep"):
    fires any enabled DiscoverySchedule whose interval_minutes cadence has
    elapsed, turning it into a normal DiscoveryScan the same way
    POST /discovery/schedules/{id}/run-now does -- except this updates
    last_run_at (run-now deliberately doesn't, so a manual trigger never
    disturbs the schedule's own timer).

    Runs each due schedule's scan synchronously, on the beat worker,
    rather than fanning out via run_network_discovery_scan_task.apply_async
    like the manual/run-now paths do: those return immediately because an
    HTTP client is waiting; this has no caller to return to; and running
    inline keeps "was this new since last time" and the ignore-rule check
    working against a DiscoveredHost set that's fully written before this
    tick moves to the next schedule, rather than racing a separate queued
    task.

    A schedule with any genuinely new host fans out a notification through
    app.services.notification_service.notify -- "new since inventory AND
    not suppressed by an existing DiscoveryIgnoreRule for this schedule"
    (run_scan already applies those rules before this runs), so a device
    someone already reviewed and dismissed doesn't keep re-alerting on
    every sweep.

    One schedule's failure (bad CIDR that somehow slipped past validation,
    a probe crash, etc.) is logged and skipped rather than aborting the
    whole tick, same as every other per-entity sweep in this file.
    Returns {"swept": N, "notified": M} for this tick.
    """
    import logging

    from app.core import crypto
    from app.models.network_discovery import (
        DiscoveredHost,
        DiscoveryScan,
        DiscoveryScanStatus,
        DiscoverySchedule,
    )
    from app.services import network_discovery_service, notification_service

    logger = logging.getLogger("netguard.tasks")
    db = SessionLocal()
    swept = 0
    notified = 0
    try:
        now = datetime.now(timezone.utc)
        candidates = db.query(DiscoverySchedule).filter(DiscoverySchedule.enabled.is_(True)).all()
        for schedule in candidates:
            if schedule.last_run_at is not None:
                elapsed_minutes = (now - schedule.last_run_at).total_seconds() / 60
                if elapsed_minutes < schedule.interval_minutes:
                    continue

            community = crypto.decrypt(schedule.snmp_community_ref) if schedule.snmp_community_ref else None
            scan = DiscoveryScan(
                cidr=schedule.cidr,
                ports=schedule.ports,
                snmp_community_ref=schedule.snmp_community_ref,
                status=DiscoveryScanStatus.RUNNING,
                started_by=f"schedule:{schedule.name}",
                schedule_id=schedule.id,
            )
            db.add(scan)
            db.commit()
            db.refresh(scan)

            try:
                network_discovery_service.run_scan(db, scan, community)
                scan.status = DiscoveryScanStatus.COMPLETED
            except Exception as exc:  # noqa: BLE001 -- one bad schedule shouldn't block the rest
                scan.status = DiscoveryScanStatus.FAILED
                scan.error = str(exc)[:500]
                logger.warning("Scheduled discovery sweep failed for schedule %s", schedule.id, exc_info=True)
                db.add(scan)
                db.commit()
                schedule.last_run_at = now
                schedule.last_scan_id = scan.id
                db.add(schedule)
                db.commit()
                continue

            scan.completed_at = datetime.now(timezone.utc)
            db.add(scan)
            db.commit()
            swept += 1

            # run_scan already applied this schedule's DiscoveryIgnoreRules,
            # marking previously-dismissed fingerprints ignored=True before
            # we get here, so this query naturally excludes them.
            new_hosts = (
                db.query(DiscoveredHost)
                .filter(
                    DiscoveredHost.scan_id == scan.id,
                    DiscoveredHost.matched_device_id.is_(None),
                    DiscoveredHost.ignored.is_(False),
                )
                .all()
            )
            if new_hosts:
                sample = ", ".join(h.ip_address for h in new_hosts[:5])
                more = f" (+{len(new_hosts) - 5} more)" if len(new_hosts) > 5 else ""
                notification_service.notify(
                    "New Device Discovered",
                    f"Schedule '{schedule.name}' ({schedule.cidr}) found {len(new_hosts)} "
                    f"new host(s) not yet in inventory: {sample}{more}",
                    severity="warning",
                )
                notified += 1

            schedule.last_run_at = now
            schedule.last_scan_id = scan.id
            db.add(schedule)
            db.commit()

        return {"swept": swept, "notified": notified}
    finally:
        db.close()

# --- Predictive / Anomaly Alerting ---------------------------------------

@celery_app.task(
    name="app.tasks.anomaly_detection_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def anomaly_detection_task(self, device_id: str) -> int:
    """Evaluates a single device's recent metric data against its
    historical baselines and raises anomaly alerts if it deviates.
    """
    from app.services import anomaly_service

    db = SessionLocal()
    try:
        device = db.get(Device, uuid.UUID(device_id))
        if device is None:
            return 0
        findings = anomaly_service.check_device_for_anomalies(db, device)
        if findings:
            raised = anomaly_service.raise_anomaly_alerts(db, device, findings)
            db.commit()
            return raised
        return 0
    finally:
        db.close()


@celery_app.task(name="app.tasks.run_anomaly_detection_sweep_task")
def run_anomaly_detection_sweep_task() -> int:
    """Celery beat entry point: fans out one anomaly_detection_task per
    device in inventory. Returns the number of devices checked.
    """
    db = SessionLocal()
    try:
        device_ids = [str(d.id) for d in db.query(Device.id).all()]
    finally:
        db.close()

    for device_id in device_ids:
        anomaly_detection_task.delay(device_id)
    return len(device_ids)


# --- Uptime & Incident Report scheduling ----------------------------------
#
# Same shape as run_weekly_compliance_report_task / run_monthly_compliance
# _report_task above: thin Celery entry points that open their own DB
# session and delegate to the service layer. Scheduled via Celery beat
# (see app.celery_app.conf.beat_schedule). The on-demand
# GET /reports/uptime-incident endpoint is unaffected by this schedule.


@celery_app.task(
    name="app.tasks.run_weekly_uptime_report_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_weekly_uptime_report_task(self) -> int:
    """Celery beat entry point: generates and emails a 7-day uptime &
    incident report for every active tenant. Returns the number of
    tenants a report was attempted for.
    """
    from app.core.config import settings
    from app.models.tenant import Tenant
    from app.services import uptime_report

    if not getattr(settings, "UPTIME_REPORT_WEEKLY_ENABLED", True):
        return 0

    db = SessionLocal()
    try:
        tenants = db.query(Tenant).filter(Tenant.is_active.is_(True)).all()
        for tenant in tenants:
            uptime_report.deliver_scheduled_report(
                db, window_days=7, period_label="Weekly", tenant_id=tenant.id
            )
        return len(tenants)
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.run_monthly_uptime_report_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def run_monthly_uptime_report_task(self) -> int:
    """Celery beat entry point: generates and emails a 30-day uptime &
    incident report for every active tenant. Returns the number of
    tenants a report was attempted for.
    """
    from app.core.config import settings
    from app.models.tenant import Tenant
    from app.services import uptime_report

    if not getattr(settings, "UPTIME_REPORT_MONTHLY_ENABLED", True):
        return 0

    db = SessionLocal()
    try:
        tenants = db.query(Tenant).filter(Tenant.is_active.is_(True)).all()
        for tenant in tenants:
            uptime_report.deliver_scheduled_report(
                db, window_days=30, period_label="Monthly", tenant_id=tenant.id
            )
        return len(tenants)
    finally:
        db.close()
