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
    # A deploy/rollback can legitimately take a while (retries + backoff in
    # deployment_engine can add up to ~14s of sleep alone, plus device I/O),
    # so give tasks headroom before Celery/the broker considers them stuck.
    task_soft_time_limit=300,
    task_time_limit=360,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Nightly configuration drift sweep (SRS: automated drift detection).
    # Runs off business hours, fanning out one drift_detection_task per
    # device (see app.tasks). On-demand scans via
    # POST /devices/{id}/drift/scan are unaffected by this schedule.
    beat_schedule={
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
    },
)

celery_app.autodiscover_tasks(["app"])