import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.api.router import api_router
from app import models  # noqa: F401  ensures models are registered on Base.metadata

logger = logging.getLogger("netguard.snmp_inprocess")

app = FastAPI(
    title=settings.APP_NAME,
    description="Intelligent Automated Network Change Management & Self-Healing Platform",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)

_snmp_poll_loop_task: asyncio.Task | None = None


async def _snmp_inprocess_poll_loop() -> None:
    """Celery/Redis-free equivalent of app.tasks.run_snmp_poll_sweep_task +
    snmp_poll_task: every SNMP_POLL_INTERVAL_SECONDS, polls every
    SNMP-enabled device and records a DeviceMetric for it. Runs directly in
    the FastAPI event loop (each poll offloaded to a worker thread via
    asyncio.to_thread since metrics_service/SNMP I/O and the DB session are
    synchronous), so the Health Dashboard / device Health tab work without
    any extra infrastructure. Guarded by SNMP_INPROCESS_POLLING_ENABLED --
    turn it off once a real Celery worker + beat are deployed so devices
    aren't polled twice on the same schedule.
    """
    from app.core.database import SessionLocal
    from app.models.device import Device
    from app.services import metrics_service

    def _poll_all_snmp_devices() -> int:
        db = SessionLocal()
        try:
            devices = db.query(Device).filter(Device.supports_snmp.is_(True)).all()
        finally:
            db.close()

        polled = 0
        for device_id in [d.id for d in devices]:
            db = SessionLocal()
            try:
                device = db.get(Device, device_id)
                if device is None:
                    continue
                try:
                    metrics_service.poll_device(db, device)
                    polled += 1
                except metrics_service.SnmpNotConfiguredError:
                    pass
                except metrics_service.credential_service.CredentialNotFoundError as exc:
                    logger.warning("SNMP poll skipped for %s: %s", device.hostname, exc)
                except Exception:  # noqa: BLE001 - one bad device must not stop the sweep
                    logger.exception("SNMP poll failed for %s", device.hostname)
            finally:
                db.close()
        return polled

    while True:
        try:
            polled = await asyncio.to_thread(_poll_all_snmp_devices)
            if polled:
                logger.info("SNMP in-process sweep polled %d device(s)", polled)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive across transient errors
            logger.exception("SNMP in-process sweep failed")
        await asyncio.sleep(settings.SNMP_POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup():
    # Schema is now owned by Alembic migrations (see backend/alembic/),
    # not this startup hook. `Base.metadata.create_all` used to run here
    # as a "prototype convenience", but it only ever creates missing
    # *tables* -- it silently never adds a column to a table that already
    # exists, which is exactly how `users.mfa_secret` ended up missing in
    # production after the model gained that column. Run
    # `alembic upgrade head` before starting the app instead (the Docker
    # image's entrypoint.sh does this automatically).

    global _snmp_poll_loop_task
    if settings.SNMP_INPROCESS_POLLING_ENABLED and _snmp_poll_loop_task is None:
        _snmp_poll_loop_task = asyncio.create_task(_snmp_inprocess_poll_loop())
        logger.info(
            "SNMP in-process polling enabled (every %ss) -- no Redis/Celery required.",
            settings.SNMP_POLL_INTERVAL_SECONDS,
        )


@app.on_event("shutdown")
async def on_shutdown():
    global _snmp_poll_loop_task
    if _snmp_poll_loop_task is not None:
        _snmp_poll_loop_task.cancel()
        _snmp_poll_loop_task = None


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}