"""Celery application instance.

Deployment pipelines run here instead of inline in the FastAPI request
thread: previously POST /change-requests/{id}/approve blocked the HTTP
response until snapshot -> deploy -> health-check -> (rollback) fully
finished for the device, which could take anywhere from seconds to minutes
and left the approving admin's browser hanging the whole time. Approve now
just enqueues one task per target device and returns immediately; the
frontend polls GET /deployments / GET /change-requests/{id} for progress.
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "netguard",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Eager mode also implicitly propagates exceptions from tasks called
    # via .delay()/.apply_async() to the caller instead of swallowing them
    # into a result backend nobody's polling -- fine here since it's only
    # used for the no-broker prototype path.
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_eager_propagates=settings.CELERY_TASK_ALWAYS_EAGER,
    # A deploy/rollback can legitimately take a while (retries + backoff in
    # deployment_engine can add up to ~14s of sleep alone, plus device I/O),
    # so give tasks headroom before Celery/the broker considers them stuck.
    task_soft_time_limit=300,
    task_time_limit=360,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # --- Beat HA ---------------------------------------------------------
    # Default Celery beat keeps its schedule + "last run" state in a local
    # file (celerybeat-schedule) and assumes exactly one beat process ever
    # runs. That's a SPOF: if that one container dies, nightly drift
    # sweeps / SNMP polling / reachability polling / compliance reports
    # all silently stop firing until someone notices and restarts it. And
    # you can't just run two file-backed beat replicas as a workaround --
    # each has its own file and its own idea of "last run", so both fire
    # every schedule independently and every polled device gets double
    # SNMP/ICMP traffic and duplicate deployments get queued twice.
    #
    # RedBeat moves the schedule into the same Redis broker this app
    # already depends on, and gates ticking behind a Redis lock
    # (`redbeat::lock`) rather than "am I the only beat process". Any
    # number of `celery beat -S redbeat.RedBeatScheduler` replicas can run
    # at once (see the `beat` service's `deploy.replicas` in
    # docker-compose.yaml); only the one holding the lock ticks. If it
    # dies, the lock's TTL expires and the next replica to check acquires
    # it and takes over -- typically within one lock-renewal interval, no
    # manual failover step.
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.CELERY_BROKER_URL,
    # How long a held lock is valid without renewal before another beat
    # replica is allowed to grab it -- i.e. worst-case gap in scheduling
    # coverage if the active replica dies mid-tick. Kept short relative to
    # the tightest schedule below (SNMP/reachability polling) so a failover
    # doesn't itself become a visible monitoring gap.
    redbeat_lock_timeout=90,
    # Discovery at Scale: keep polling traffic off the default queue so it
    # can be capacity-limited independently of deployment/drift/report
    # tasks -- see the `poller` service in docker/docker-compose.yml, which is
    # the only consumer of this queue and runs with its own
    # --concurrency limit distinct from the general `worker` service.
    task_routes={
        "app.tasks.snmp_poll_task": {"queue": "polling"},
        "app.tasks.reachability_task": {"queue": "polling"},
        # Deployment Pipeline (Approved -> Snapshot -> Deploy -> Health
        # Monitor -> Success | Rollback -> Notify), SRS 6.6/6.8. This is
        # every task actually dispatched by POST /change-requests/{id}/approve
        # and POST /deployments/{id}/retry -- the per-device work
        # (deploy_device_task, retry_deployment_task), the canary gate
        # that runs between the first device and the rest of the fleet
        # (canary_gate_task), and the chord callback that finalizes the
        # change request once every device is done (finalize_change_request_task).
        # run_deployment_pipeline_task itself is just the lightweight
        # dispatcher that fans these out -- routing it too keeps the
        # whole pipeline off the shared `celery` queue, so a large
        # multi-device change request can't queue behind (or get queued
        # behind by) a slow compliance report, and vice versa. All
        # consumed only by the `deployer` service (see
        # docker/docker-compose.yml's `deployer` service).
        "app.tasks.run_deployment_pipeline_task": {"queue": "deploy"},
        "app.tasks.deploy_device_task": {"queue": "deploy"},
        "app.tasks.retry_deployment_task": {"queue": "deploy"},
        "app.tasks.canary_gate_task": {"queue": "deploy"},
        "app.tasks.finalize_change_request_task": {"queue": "deploy"},
        # Firmware upgrades hold a device session for the duration of a
        # flash/reboot cycle, same shape as a deployment -- keep it off
        # the shared queue too so a firmware job can't block drift/report
        # tasks, or vice versa. Not routed to `deploy` itself: firmware
        # upgrades are rarer and longer-running than config deploys, so
        # giving them their own queue keeps a firmware backlog from
        # delaying in-flight change requests.
        "app.tasks.run_firmware_upgrade_task": {"queue": "firmware"},
    },
    beat_schedule={
        # Nightly configuration drift sweep (SRS: automated drift detection).
        # Runs off business hours, fanning out one drift_detection_task per
        # device (see app.tasks). On-demand scans via
        # POST /devices/{id}/drift/scan are unaffected by this schedule.
        "nightly-drift-sweep": {
            "task": "app.tasks.run_nightly_drift_sweep_task",
            "schedule": crontab(hour=settings.DRIFT_SWEEP_HOUR_UTC, minute=0),
        },
        # SNMP Monitoring / Health Dashboard: fans out one snmp_poll_task
        # per SNMP-enabled device every SNMP_POLL_INTERVAL_SECONDS so the
        # dashboard, health scores, and historical charts stay current.
        "snmp-poll-sweep": {
            "task": "app.tasks.run_snmp_poll_sweep_task",
            "schedule": float(settings.SNMP_POLL_INTERVAL_SECONDS),
        },
        # Device reachability (ping) sweep: keeps Device.status accurate
        # for every device, not just SNMP-enabled ones -- see
        # app.services.reachability_service for why this exists (status
        # was previously only ever set for GNS3-imported lab devices).
        "reachability-sweep": {
            "task": "app.tasks.run_reachability_sweep_task",
            "schedule": float(settings.REACHABILITY_POLL_INTERVAL_SECONDS),
        },
        # Compliance report scheduling: turns GET /reports/compliance from
        # something someone has to remember to pull into a recurring
        # artifact emailed to NOTIFY_EMAIL_RECIPIENTS (see
        # app.services.compliance_report.deliver_scheduled_report). Each
        # task no-ops (returns False without building/sending anything) if
        # its COMPLIANCE_REPORT_*_ENABLED flag is off, so both stay
        # registered here and the toggle lives entirely in settings.
        "weekly-compliance-report": {
            "task": "app.tasks.run_weekly_compliance_report_task",
            "schedule": crontab(day_of_week="mon", hour=settings.COMPLIANCE_REPORT_HOUR_UTC, minute=0),
        },
        "monthly-compliance-report": {
            "task": "app.tasks.run_monthly_compliance_report_task",
            "schedule": crontab(day_of_month=1, hour=settings.COMPLIANCE_REPORT_HOUR_UTC, minute=0),
        },
        # Configuration Snapshot retention sweep: enforces
        # SNAPSHOT_RETENTION_DAYS / SNAPSHOT_RETENTION_MIN_PER_DEVICE
        # nightly so snapshot history (taken automatically before every
        # deployment/restore/rollback, on top of on-demand backups)
        # doesn't grow unbounded. See app.services.snapshot_service.
        "snapshot-retention-sweep": {
            "task": "app.tasks.run_snapshot_retention_task",
            "schedule": crontab(hour=settings.SNAPSHOT_RETENTION_SWEEP_HOUR_UTC, minute=0),
        },
        # Escalation Policies: scans unacknowledged alerts and notifies
        # secondary/on-call contacts for anything that's breached an
        # enabled policy's unack_minutes threshold. See
        # app.services.escalation_service.
        "alert-escalation-sweep": {
            "task": "app.tasks.run_escalation_sweep_task",
            "schedule": float(settings.ESCALATION_SWEEP_INTERVAL_SECONDS),
        },
        # JIT Access: warns holders whose grant is about to lapse
        # (JIT_EXPIRY_WARNING_MINUTES out) and notifies when a grant
        # actually expires, via the standard Slack/Teams/webhook/email/
        # in-app fan-out -- previously a grant just silently stopped
        # working with no notice, see app.services.jit_service.
        # sweep_expiry_notifications.
        "jit-expiry-notify-sweep": {
            "task": "app.tasks.run_jit_expiry_notify_sweep_task",
            "schedule": float(settings.JIT_EXPIRY_SWEEP_INTERVAL_SECONDS),
        },
        # Approval SLA Slack/Teams reminders: posts a "due soon"/"overdue"
        # countdown for any PENDING_APPROVAL change request that just
        # crossed a new SLA stage, so approvers see it where they work
        # instead of only in the in-app pending-approvals queue. See
        # app.services.approval_sla_notifier_service.
        "approval-sla-notify-sweep": {
            "task": "app.tasks.run_approval_sla_notify_sweep_task",
            "schedule": float(settings.APPROVAL_SLA_NOTIFY_SWEEP_INTERVAL_SECONDS),
        },
        # Recurring maintenance windows: materializes the next horizon of
        # concrete MaintenanceWindow rows for every enabled
        # RecurringMaintenanceSchedule once a day. Idempotent, so a
        # missed/late tick just catches up on the next run. See
        # app.services.recurring_window_service.
        "recurring-maintenance-window-generation": {
            "task": "app.tasks.run_recurring_window_generation_task",
            "schedule": crontab(hour=settings.RECURRING_WINDOW_GENERATION_HOUR_UTC, minute=0),
        },
        # IPAM scheduled re-scan: re-runs the nmap ping-sweep for any
        # Subnet with auto_rescan_enabled whose own cadence has elapsed
        # (see app.services.ipam_service.due_for_rescan), same
        # per-entity-cadence-behind-a-fixed-tick shape as
        # reachability-sweep/snmp-poll-sweep above. See
        # app.tasks.run_subnet_rescan_sweep_task.
        "ipam-subnet-rescan-sweep": {
            "task": "app.tasks.run_subnet_rescan_sweep_task",
            "schedule": float(settings.IPAM_RESCAN_SWEEP_INTERVAL_SECONDS),
        },
        # IPAM conflict alerting: turns ipam_service.fleet_conflicts from
        # a pull-only check (someone has to open the IPAM page) into a
        # real alert in the same pipeline topology snapshot diffs use.
        # See app.tasks.run_ipam_conflict_alert_sweep_task.
        "ipam-conflict-alert-sweep": {
            "task": "app.tasks.run_ipam_conflict_alert_sweep_task",
            "schedule": float(settings.IPAM_CONFLICT_ALERT_SWEEP_INTERVAL_SECONDS),
        },
        # GitOps: safety-net periodic re-pull for any auto_sync_enabled
        # repo, in case its webhook was never configured or a delivery
        # failed -- see app.tasks.run_gitops_auto_sync_sweep_task.
        "gitops-auto-sync-sweep": {
            "task": "app.tasks.run_gitops_auto_sync_sweep_task",
            "schedule": float(settings.GITOPS_AUTO_SYNC_INTERVAL_SECONDS),
        },
    },
)

celery_app.autodiscover_tasks(["app"])
