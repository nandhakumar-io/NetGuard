"""Publishes device-operation jobs to the Device Gateway over NATS and
waits for the result -- this is what API code should call INSTEAD OF
app.services.protocol_manager / netconf_service / deployment_engine
directly, for any code path that talks to a real network device.

Why this exists (Section 3 of the hardening spec): the API process must
not itself hold network-device connectivity or decrypt device
credentials. This module's job is narrow -- build a signed, short-lived,
declarative job envelope and hand it to NATS -- it never opens a socket
to a device and never touches a device credential.

Migration note: `terminal.py`'s interactive SSH/Telnet path has since
been migrated too -- it no longer imports asyncssh/telnetlib3 at all and
instead relays over `terminal.open`/`terminal.session.*` subjects to
app.device_gateway.terminal_executor (see that module and
app.schemas.terminal_job). It doesn't use this module's request/response
`submit_job` helper because a streaming session doesn't fit that shape,
but it follows the same pattern: sign a short-lived envelope, let the
Gateway independently re-validate and be the only process that resolves
a device credential or opens a device-facing socket.

Remaining known call sites that still reach `protocol_manager` /
`netconf_service` / `deployment_engine` directly from API/worker code
(as opposed to via a Gateway job) have not yet been audited as part of
this pass -- see the open item to trace whether they're dead code left
over from before the Gateway existed, or still-live paths that need the
same migration `submit_job` and Terminal both received.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import nats

from app.core.config import settings
from app.schemas.device_job import (
    DeviceJobRequest,
    DeviceJobResult,
    DeviceOperation,
    sign,
)

logger = logging.getLogger("netguard.device_job_service")

JOBS_REQUEST_SUBJECT = "jobs.request"
JOBS_RESULT_SUBJECT_PREFIX = "jobs.result"  # + "." + job_id, so each waiter only hears its own result

DEFAULT_JOB_TTL_SECONDS = 120


class DeviceJobTimeoutError(Exception):
    pass


class DeviceJobFailedError(Exception):
    def __init__(self, error: str | None):
        self.error = error
        super().__init__(error or "device job failed")


async def submit_job(
    *,
    tenant_id: str,
    device_id: str,
    operation: DeviceOperation,
    params: dict,
    requested_by: str,
    change_request_id: str | None = None,
    approval_id: str | None = None,
    jit_elevation_id: str | None = None,
    ttl_seconds: int = DEFAULT_JOB_TTL_SECONDS,
    timeout_seconds: float = 30.0,
) -> DeviceJobResult:
    """Builds, signs, and publishes a job; waits for the matching result
    on jobs.result.<job_id>. Raises DeviceJobTimeoutError if the Gateway
    never responds (down, network issue, or -- most importantly -- it
    rejected the job during independent validation and simply never ran
    it) and DeviceJobFailedError if the Gateway ran it and it failed.
    """
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    job = DeviceJobRequest(
        job_id=job_id,
        tenant_id=tenant_id,
        device_id=device_id,
        operation=operation,
        params=params,
        requested_by=requested_by,
        change_request_id=change_request_id,
        approval_id=approval_id,
        jit_elevation_id=jit_elevation_id,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    job = sign(job, settings.DEVICE_JOB_SIGNING_KEY)

    nc = await nats.connect(
        servers=[settings.NATS_URL],
        name="netguard-api-job-publisher",
        user=settings.NATS_API_USER,
        password=settings.NATS_API_PASSWORD,
    )
    try:
        result_subject = f"{JOBS_RESULT_SUBJECT_PREFIX}.{job_id}"
        sub = await nc.subscribe(result_subject)
        try:
            await nc.publish(JOBS_REQUEST_SUBJECT, job.model_dump_json().encode("utf-8"))
            try:
                msg = await sub.next_msg(timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise DeviceJobTimeoutError(
                    f"Device Gateway did not respond to job {job_id} within {timeout_seconds}s "
                    "(job may have been rejected during independent validation, or the Gateway "
                    "is unavailable)"
                ) from exc
            result = DeviceJobResult(**json.loads(msg.data.decode("utf-8")))
        finally:
            with __import__("contextlib").suppress(Exception):
                await sub.unsubscribe()
    finally:
        await nc.close()

    if not result.success:
        raise DeviceJobFailedError(result.error)
    return result


def submit_job_sync(**kwargs) -> DeviceJobResult:
    """Sync-context bridge to submit_job() for callers that are plain
    `def` functions rather than `async def` -- e.g. pipeline_service's
    Celery-task-driven deployment/rollback/monitoring code, which was
    already calling this function under this exact name before it
    existed here. (Every DEVICE_GATEWAY_ENABLED call site that went
    through pipeline_service was raising AttributeError at the point of
    call until this was added -- there was no working sync-context path
    from the Gateway migration to any Celery-task caller.)

    Not usable from inside a running event loop (e.g. an async FastAPI
    request handler) -- asyncio.run() cannot nest. Those callers should
    `await submit_job(...)` directly; this raises RuntimeError rather
    than silently deadlocking or corrupting an unrelated loop if
    misused from such a context.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # good -- no loop running, safe to drive our own
    else:
        raise RuntimeError(
            "submit_job_sync() was called from inside a running event loop; "
            "await submit_job() directly instead of using the sync wrapper"
        )
    return asyncio.run(submit_job(**kwargs))
