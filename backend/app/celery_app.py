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
    # Config Drift Detection: continuous monitoring independent of any
    # deployment (SRS) -- sweeps every device on a schedule so out-of-band
    # changes are caught even when nothing is being deployed. Run
    # `celery -A app.celery_app.celery_app beat` alongside the worker(s)
    # to actually fire this.
    beat_schedule={
        "sweep-all-devices-for-drift": {
            "task": "app.tasks.sweep_all_devices_for_drift_task",
            "schedule": 900.0,  # every 15 minutes
        },
    },
)

celery_app.autodiscover_tasks(["app"])
