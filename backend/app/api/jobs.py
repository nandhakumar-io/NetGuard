"""Background job visibility (Celery task queue).

deployment_engine/pipeline_service run their actual work as Celery tasks
(app.tasks), and per-device deployment status already has full visibility
via GET /deployments + the Deployments page. What's *not* visible anywhere
is the other half of the Celery surface: the periodic sweeps registered in
celery_app.beat_schedule (drift, SNMP/reachability polling, escalation,
snapshot retention, compliance reports) and whatever's actually in-flight
or queued on the worker fleet right now -- there's no page today that
answers "is a worker even running" or "is a task stuck".

This endpoint gives that a home: the static beat schedule (so it's visible
even with zero workers up) plus a live `celery.control.inspect()` snapshot
of active/reserved/scheduled tasks per worker. Inspect calls are a
broadcast RPC over the broker with a short timeout, so this endpoint can be
slow-ish (up to ~2s) if a worker is unresponsive -- deliberately not called
from the dashboard/websocket hot path.
"""
import datetime

from fastapi import APIRouter, Depends

from app.celery_app import celery_app
from app.core.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Celery's crontab/schedule objects don't serialize to JSON directly, and
# their human_seconds()/__repr__ differ in verbosity -- just describe each
# entry's cadence here, once, next to its beat_schedule definition rather
# than trying to introspect it generically.
_SCHEDULE_DESCRIPTIONS = {
    "nightly-drift-sweep": "Nightly, off business hours",
    "snmp-poll-sweep": "Every SNMP_POLL_INTERVAL_SECONDS",
    "reachability-sweep": "Every REACHABILITY_POLL_INTERVAL_SECONDS",
    "weekly-compliance-report": "Mondays",
    "monthly-compliance-report": "1st of the month",
    "snapshot-retention-sweep": "Nightly",
    "alert-escalation-sweep": "Every ESCALATION_SWEEP_INTERVAL_SECONDS",
}


def _task_entry(task: dict) -> dict:
    """Normalizes one entry from inspect().active()/reserved()/scheduled()
    -- scheduled() nests the actual task under "request" and adds an ETA,
    everything else is flat, so this handles both shapes.
    """
    request = task.get("request", task)
    return {
        "id": request.get("id"),
        "name": request.get("name") or request.get("type"),
        "args": request.get("args"),
        "kwargs": request.get("kwargs"),
        "eta": task.get("eta"),
        "time_start": request.get("time_start"),
    }


@router.get("")
def list_jobs(_: User = Depends(get_current_user)):
    schedule = [
        {
            "name": name,
            "task": entry["task"],
            "cadence": _SCHEDULE_DESCRIPTIONS.get(name, "See celery_app.py"),
        }
        for name, entry in celery_app.conf.beat_schedule.items()
    ]

    workers: dict = {}
    inspect_error: str | None = None
    try:
        inspector = celery_app.control.inspect(timeout=2.0)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        worker_names = set(active) | set(reserved) | set(scheduled)
        for worker in worker_names:
            workers[worker] = {
                "active": [_task_entry(t) for t in active.get(worker, [])],
                "reserved": [_task_entry(t) for t in reserved.get(worker, [])],
                "scheduled": [_task_entry(t) for t in scheduled.get(worker, [])],
            }
    except Exception as exc:  # noqa: BLE001 -- broker down/unreachable is a valid, displayable state
        inspect_error = str(exc)

    return {
        "checked_at": datetime.datetime.now(datetime.timezone.utc),
        "workers_online": len(workers),
        "workers": workers,
        "beat_schedule": schedule,
        "inspect_error": inspect_error,
    }
