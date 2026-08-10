import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models  # noqa: F401  ensures models are registered on Base.metadata
from app.api.router import api_router
from app.core.config import settings

logger = logging.getLogger("netguard.snmp_inprocess")

_snmp_poll_loop_task: asyncio.Task | None = None
_syslog_transport = None  # asyncio.DatagramTransport | None -- see app.services.syslog_service
_flow_transport = None  # asyncio.DatagramTransport | None -- see app.services.flow_service (NetFlow/IPFIX)
_sflow_transport = None  # asyncio.DatagramTransport | None -- see app.services.flow_service (sFlow)
_topology_snapshot_loop_task: asyncio.Task | None = None


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
                except Exception:
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
        except Exception:
            logger.exception("SNMP in-process sweep failed")
        await asyncio.sleep(settings.SNMP_POLL_INTERVAL_SECONDS)


async def _topology_snapshot_loop() -> None:
    """Periodically captures a TopologySnapshot so the Topology page can
    diff "now" against "settings.TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS ago"
    -- see app.services.topology_service.capture_snapshot /
    diff_snapshots and GET /topology/diff.
    """
    from app.core.database import SessionLocal
    from app.services import topology_service

    def _capture() -> None:
        db = SessionLocal()
        try:
            topology_service.capture_snapshot(db)
        finally:
            db.close()

    while True:
        try:
            await asyncio.to_thread(_capture)
            logger.info("Captured topology snapshot")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Topology snapshot capture failed")
        await asyncio.sleep(settings.TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly before accepting any traffic if ENVIRONMENT=production
    # is set but SECRET_KEY/SECRET_ENCRYPTION_KEY/CORS_ALLOWED_ORIGINS are
    # still on insecure dev defaults -- see Settings.validate_production_secrets.
    settings.validate_production_secrets()

    # Schema is owned by Alembic migrations (see backend/alembic/). The
    # Docker image's entrypoint.sh runs `alembic upgrade head` before
    # starting uvicorn -- but when running locally with `uvicorn
    # app.main:app` directly (no entrypoint.sh in the loop), that step
    # gets skipped and the DB silently drifts behind the models, which is
    # exactly what caused `relation "golden_configs" does not exist`, etc.
    # Apply any pending migrations here too so local/dev runs stay in sync
    # automatically. This is idempotent -- alembic no-ops if already at head.
    try:
        from alembic.config import Config as AlembicConfig

        from alembic import command

        backend_dir = Path(__file__).resolve().parent.parent
        alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
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

    global _syslog_transport
    if settings.SYSLOG_LISTENER_ENABLED and _syslog_transport is None:
        from app.services import syslog_service

        _syslog_transport = await syslog_service.start_syslog_listener(
            host=settings.SYSLOG_UDP_HOST, port=settings.SYSLOG_UDP_PORT
        )

    global _flow_transport
    if settings.NETFLOW_LISTENER_ENABLED and _flow_transport is None:
        from app.services import flow_service

        _flow_transport = await flow_service.start_flow_listener(
            host=settings.NETFLOW_UDP_HOST, port=settings.NETFLOW_UDP_PORT
        )

    global _sflow_transport
    if settings.SFLOW_LISTENER_ENABLED and _sflow_transport is None:
        from app.services import flow_service

        _sflow_transport = await flow_service.start_sflow_listener(
            host=settings.SFLOW_UDP_HOST, port=settings.SFLOW_UDP_PORT
        )

    global _topology_snapshot_loop_task
    if settings.TOPOLOGY_SNAPSHOT_ENABLED and _topology_snapshot_loop_task is None:
        _topology_snapshot_loop_task = asyncio.create_task(_topology_snapshot_loop())

    yield

    if _snmp_poll_loop_task is not None:
        _snmp_poll_loop_task.cancel()
        _snmp_poll_loop_task = None

    if _syslog_transport is not None:
        _syslog_transport.close()
        _syslog_transport = None

    if _flow_transport is not None:
        _flow_transport.close()
        _flow_transport = None

    if _sflow_transport is not None:
        _sflow_transport.close()
        _sflow_transport = None

    if _topology_snapshot_loop_task is not None:
        _topology_snapshot_loop_task.cancel()
        _topology_snapshot_loop_task = None

app = FastAPI(
    title=settings.APP_NAME,
    description="Intelligent Automated Network Change Management & Self-Healing Platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
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

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}
