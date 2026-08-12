"""Standalone syslog collector process.

Run with: uvicorn app.collectors.syslog_collector:app --host 0.0.0.0 --port 8000

Owns exactly one thing: the syslog UDP listener (app.services.syslog_service
.start_syslog_listener), extracted out of app.main's lifespan so it can be
deployed, scaled, and restarted independently of the api tier -- see
app/collectors/__init__.py for the full rationale. POST /syslog/ingest (the
TCP-forwarder / HTTP-push entry point into the same ingest_message()
pipeline) still lives on the api tier as a normal REST route; this process
only ever adds the UDP path.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger("netguard.collectors.syslog")

_transport = None  # asyncio.DatagramTransport | None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _transport
    from app.services import syslog_service

    if settings.SYSLOG_LISTENER_ENABLED:
        _transport = await syslog_service.start_syslog_listener(
            host=settings.SYSLOG_UDP_HOST, port=settings.SYSLOG_UDP_PORT
        )
        if _transport is None:
            # start_syslog_listener already logs the bind failure -- this
            # process has no other job, so a failed bind means it's doing
            # nothing at all. Fail loudly (crash -> container restarts)
            # rather than idling forever as a healthy-looking no-op.
            raise RuntimeError(
                f"syslog-collector: could not bind UDP {settings.SYSLOG_UDP_HOST}:"
                f"{settings.SYSLOG_UDP_PORT} -- refusing to start as a no-op listener."
            )
    else:
        logger.warning(
            "SYSLOG_LISTENER_ENABLED is false -- this container has nothing to do. "
            "Set it true, or don't run this service."
        )

    yield

    if _transport is not None:
        _transport.close()
        _transport = None


app = FastAPI(title="NetGuard Syslog Collector", lifespan=lifespan)


@app.get("/health")
@app.get("/healthz")
def health():
    return {"status": "healthy"}


@app.get("/readyz")
def readyz():
    """Ready means the UDP socket is actually bound, not just that the
    process is up -- a bind failure raises during startup (see lifespan
    above) so in practice this only ever reports not-ready in the brief
    window before that, but it's a real functional check rather than a
    hardcoded 200 like /healthz.
    """
    ready = _transport is not None
    body = {"status": "ready" if ready else "not_ready", "udp_listener": ready}
    return JSONResponse(status_code=200 if ready else 503, content=body)
