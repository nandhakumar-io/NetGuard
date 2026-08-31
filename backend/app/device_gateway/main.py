"""Device Gateway entrypoint: subscribes to `jobs.request` on NATS,
independently validates each job, executes it, publishes the result to
`jobs.result.<job_id>`, and writes an audit record for every outcome —
accepted or rejected. This is the only NetGuard process meant to run
with network-management connectivity and OpenBao device-credential
access (see docker-compose.yaml's `device-gateway` service and
`netguard-secrets` / `netguard-execution` networks).

Deliberately a plain asyncio consume loop, not a Celery worker: job
handling here must be a tight validate -> execute -> respond path with
no shared task queue/broker in common with the rest of the app (that
separation is part of the trust boundary, not just a style choice).
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

import nats

from app.core.config import settings
from app.core.database import SessionLocal
from app.device_gateway import executor, terminal_executor, validator
from app.schemas.device_job import DeviceJobRequest, DeviceJobResult
from app.schemas.terminal_job import (
    TERMINAL_OPEN_SUBJECT,
    TERMINAL_RESULT_SUBJECT_PREFIX,
    TerminalOpenRequest,
    TerminalOpenResult,
)
from app.services import audit_service

logger = logging.getLogger("netguard.device_gateway.main")

JOBS_REQUEST_SUBJECT = "jobs.request"
JOBS_RESULT_SUBJECT_PREFIX = "jobs.result"

# Terminal sessions run for a long time (up to terminal_executor's own
# idle timeout) and are user-interactive, so they are NOT put through the
# same MAX_CONCURRENT_JOBS semaphore as one-shot config jobs -- one slow
# operator typing in a shell must never block config-deploy jobs from
# being picked up. Bounded separately instead.
MAX_CONCURRENT_TERMINALS = 20

# Bounded so a burst of jobs can't exhaust the Gateway's own resources —
# this process talks directly to devices, so it deliberately does not
# try to be a high-throughput queue consumer.
MAX_CONCURRENT_JOBS = 8


async def _handle_message(msg, semaphore: asyncio.Semaphore, nc) -> None:
    async with semaphore:
        db = SessionLocal()
        job: DeviceJobRequest | None = None
        try:
            job = DeviceJobRequest.model_validate_json(msg.data)
        except Exception as exc:  # noqa: BLE001 - malformed input, never crash the loop
            logger.warning("device_gateway: dropped unparseable job message: %s", exc)
            db.close()
            return

        try:
            device = validator.validate(job, db, settings.DEVICE_JOB_SIGNING_KEY)
        except validator.JobRejected as exc:
            logger.warning("device_gateway: rejected job %s: %s", job.job_id, exc)
            audit_service.record_event(
                db,
                actor=f"user:{job.requested_by}",
                action=f"device_job.{job.operation.value}",
                result="rejected",
                device_hostname=job.device_id,
                change_request_id=job.change_request_id,
                detail=str(exc),
                tenant_id=job.tenant_id,
            )
            result = DeviceJobResult(
                job_id=job.job_id,
                success=False,
                error=f"rejected: {exc}",
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
            await nc.publish(
                f"{JOBS_RESULT_SUBJECT_PREFIX}.{job.job_id}", result.model_dump_json().encode("utf-8")
            )
            db.close()
            return

        try:
            result = executor.execute(job, device, db)
        finally:
            audit_service.record_event(
                db,
                actor=f"user:{job.requested_by}",
                action=f"device_job.{job.operation.value}",
                result="success" if result.success else "failure",
                device_hostname=device.hostname,
                change_request_id=job.change_request_id,
                detail=result.error or "",
                tenant_id=job.tenant_id,
            )

        await nc.publish(
            f"{JOBS_RESULT_SUBJECT_PREFIX}.{job.job_id}", result.model_dump_json().encode("utf-8")
        )
        db.close()


async def _handle_terminal_open(msg, terminal_semaphore: asyncio.Semaphore, nc) -> None:
    db = SessionLocal()
    req: TerminalOpenRequest | None = None
    try:
        req = TerminalOpenRequest.model_validate_json(msg.data)
    except Exception as exc:  # noqa: BLE001 - malformed input, never crash the loop
        logger.warning("device_gateway: dropped unparseable terminal-open message: %s", exc)
        db.close()
        return

    try:
        device, user = validator.validate_terminal_open(req, db, settings.DEVICE_JOB_SIGNING_KEY)
    except validator.JobRejected as exc:
        logger.warning("device_gateway: rejected terminal open %s: %s", req.session_id, exc)
        audit_service.record_event(
            db, actor=f"user:{req.requested_by}", action="terminal.open", result="rejected",
            device_hostname=req.device_id, detail=str(exc), tenant_id=req.tenant_id,
        )
        result = TerminalOpenResult(session_id=req.session_id, accepted=False, error=f"rejected: {exc}")
        await nc.publish(
            f"{TERMINAL_RESULT_SUBJECT_PREFIX}.{req.session_id}", result.model_dump_json().encode("utf-8")
        )
        db.close()
        return

    audit_service.record_event(
        db, actor=user.email, action="terminal.open", result="accepted",
        device_hostname=device.hostname, tenant_id=req.tenant_id,
    )
    result = TerminalOpenResult(session_id=req.session_id, accepted=True)
    await nc.publish(f"{TERMINAL_RESULT_SUBJECT_PREFIX}.{req.session_id}", result.model_dump_json().encode("utf-8"))

    username = device.ssh_username or "admin"
    hostname, requested_by = device.hostname, req.requested_by
    db.close()  # run_session opens its own DB session internally (long-lived)

    async with terminal_semaphore:
        try:
            await terminal_executor.run_session(nc, req.session_id, device, username, requested_by)
        finally:
            audit_db = SessionLocal()
            try:
                audit_service.record_event(
                    audit_db, actor=user.email, action="terminal.closed", result="success",
                    device_hostname=hostname, tenant_id=req.tenant_id,
                )
            finally:
                audit_db.close()


async def run() -> None:
    settings.validate_production_secrets() if hasattr(settings, "validate_production_secrets") else None

    nc = await nats.connect(servers=[settings.NATS_URL], name="netguard-device-gateway")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    terminal_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TERMINALS)

    async def _cb(msg):
        # Fire-and-forget per message so one slow device call doesn't
        # block the subscription's delivery of the next job; concurrency
        # is bounded by the semaphore, not by NATS delivery.
        asyncio.create_task(_handle_message(msg, semaphore, nc))

    async def _terminal_cb(msg):
        asyncio.create_task(_handle_terminal_open(msg, terminal_semaphore, nc))

    sub = await nc.subscribe(JOBS_REQUEST_SUBJECT, cb=_cb)
    terminal_sub = await nc.subscribe(TERMINAL_OPEN_SUBJECT, cb=_terminal_cb)
    logger.info(
        "device_gateway: subscribed to %s and %s, awaiting jobs",
        JOBS_REQUEST_SUBJECT, TERMINAL_OPEN_SUBJECT,
    )

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    logger.info("device_gateway: shutting down")
    await sub.unsubscribe()
    await terminal_sub.unsubscribe()
    await nc.drain()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
