import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for anything a route/dependency raises that isn't already
    an HTTPException.

    Without this, an unhandled exception propagates past FastAPI's
    ExceptionMiddleware (which sits *inside* CORSMiddleware in the
    Starlette stack) all the way out to Starlette's ServerErrorMiddleware,
    which sits *outside* CORSMiddleware and generates its own fallback 500
    -- one with no CORS headers on it, because it never passes back
    through CORSMiddleware's response-wrapping. The browser then can't
    read that response at all (it fails the CORS check), so axios/fetch
    reports it as an opaque "Network Error" with no status code or body
    -- exactly what device delete, alert clearing, or any other endpoint
    looks like in the UI whenever something throws that the endpoint
    itself didn't anticipate and catch.

    Registering a handler here means FastAPI's ExceptionMiddleware catches
    the exception itself and calls this handler *before* the request ever
    reaches ServerErrorMiddleware -- so the JSONResponse below still flows
    back out through CORSMiddleware and gets proper headers, and the
    frontend gets a real 500 + JSON body it can actually show the user
    instead of a dead end.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {exc}"},
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
    # Schema is owned by Alembic migrations (see backend/alembic/). The
    # Docker image's entrypoint.sh runs `alembic upgrade head` before
    # starting uvicorn -- but when running locally with `uvicorn
    # app.main:app` directly (no entrypoint.sh in the loop), that step
    # gets skipped and the DB silently drifts behind the models, which is
    # exactly what caused `relation "golden_configs" does not exist`, etc.
    # Apply any pending migrations here too so local/dev runs stay in sync
    # automatically. This is idempotent -- alembic no-ops if already at head.
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        backend_dir = Path(__file__).resolve().parent.parent
        alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
        # alembic.ini's script_location is the relative path "alembic",
        # which Alembic resolves against the current working directory --
        # NOT against the ini file's own directory. If this process wasn't
        # started with CWD=backend/ (e.g. launched via a process manager,
        # systemd, or `uvicorn app.main:app` from the repo root), that
        # relative lookup silently fails to find alembic/versions/, and
        # migrations never actually run even though this block appears to
        # succeed. Force it to an absolute path so it's correct regardless
        # of CWD.
        alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
        logger.info("Alembic migrations applied (upgrade head).")
    except Exception:
        logger.exception(
            "Auto-migration on startup FAILED -- the app is very likely running "
            "against an out-of-date schema right now (missing tables/columns "
            "will surface as 500s on random endpoints, e.g. device delete or "
            "SNMP setup). Run `alembic upgrade head` manually from backend/ "
            "and restart."
        )

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