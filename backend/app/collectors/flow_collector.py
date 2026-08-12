"""Standalone NetFlow/IPFIX + sFlow collector process.

Run with: uvicorn app.collectors.flow_collector:app --host 0.0.0.0 --port 8000

Owns both flow listeners (app.services.flow_service.start_flow_listener /
start_sflow_listener), extracted out of app.main's lifespan -- see
app/collectors/__init__.py for the full rationale. Both listeners live in
this one process (not split into two) because NetFlow v9/IPFIX template
decoding is stateful in-process (flow_service._TEMPLATES, keyed by
(exporter, template_id)); that's an implementation detail of
flow_service.py, not something this module needs to know about, but it's
why "one flow collector" rather than "one per protocol" is the right unit
here. sFlow has no such shared state, so splitting sFlow out later would
be a smaller change than splitting NetFlow/IPFIX would be.

Either listener failing to bind independently disables just that
protocol rather than crashing the process -- unlike the syslog collector,
losing one of two protocols still leaves this container doing useful
work, so it stays up and reports which one failed via /readyz.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger("netguard.collectors.flow")

_flow_transport = None  # asyncio.DatagramTransport | None -- NetFlow/IPFIX
_sflow_transport = None  # asyncio.DatagramTransport | None -- sFlow


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _flow_transport, _sflow_transport
    from app.services import flow_service

    if settings.NETFLOW_LISTENER_ENABLED:
        _flow_transport = await flow_service.start_flow_listener(
            host=settings.NETFLOW_UDP_HOST, port=settings.NETFLOW_UDP_PORT
        )

    if settings.SFLOW_LISTENER_ENABLED:
        _sflow_transport = await flow_service.start_sflow_listener(
            host=settings.SFLOW_UDP_HOST, port=settings.SFLOW_UDP_PORT
        )

    if not settings.NETFLOW_LISTENER_ENABLED and not settings.SFLOW_LISTENER_ENABLED:
        logger.warning(
            "NETFLOW_LISTENER_ENABLED and SFLOW_LISTENER_ENABLED are both false -- "
            "this container has nothing to do. Enable at least one, or don't run this service."
        )
    elif _flow_transport is None and _sflow_transport is None:
        # Both enabled-but-failed (or the one enabled protocol failed) --
        # same reasoning as syslog_collector: no point staying up as a
        # silent no-op.
        raise RuntimeError(
            "flow-collector: no listener bound successfully (see preceding bind-failure "
            "log lines) -- refusing to start as a no-op."
        )

    yield

    if _flow_transport is not None:
        _flow_transport.close()
        _flow_transport = None
    if _sflow_transport is not None:
        _sflow_transport.close()
        _sflow_transport = None


app = FastAPI(title="NetGuard Flow Collector", lifespan=lifespan)


@app.get("/health")
@app.get("/healthz")
def health():
    return {"status": "healthy"}


@app.get("/readyz")
def readyz():
    checks = {
        "netflow_listener": _flow_transport is not None,
        "sflow_listener": _sflow_transport is not None,
    }
    # Ready if every *enabled* listener actually bound -- a protocol that's
    # deliberately disabled via settings shouldn't count against readiness.
    ready = (not settings.NETFLOW_LISTENER_ENABLED or checks["netflow_listener"]) and (
        not settings.SFLOW_LISTENER_ENABLED or checks["sflow_listener"]
    )
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ready else 503, content=body)
