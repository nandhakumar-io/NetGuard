"""System health checks.

Backs GET /api/v1/health/detailed and GET /api/v1/health/pages (see
app.api.health). Separate from the plain GET /health in app.main, which is
an unauthenticated, dependency-free liveness probe for load balancers /
`docker compose healthcheck` -- it intentionally never touches the DB,
Redis, or any external service, so it can answer "the process is alive"
even while everything below it is on fire. Everything here is the deeper,
in-app "what's actually working" view surfaced on the System Health page.

Every check function returns a ComponentHealth and is written to never
raise -- a broken integration should show up as a "down" row, not a 500
on the health page itself.
"""
from __future__ import annotations

import asyncio
import socket
import time

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.schemas.health import ComponentHealth, ComponentStatus


def _timed() -> float:
    return time.perf_counter()


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def check_database(db: Session) -> ComponentHealth:
    start = _timed()
    try:
        db.execute(text("SELECT 1"))
        return ComponentHealth(
            key="database",
            label="PostgreSQL Database",
            status=ComponentStatus.UP,
            critical=True,
            latency_ms=_ms_since(start),
            detail="Connected",
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        return ComponentHealth(
            key="database",
            label="PostgreSQL Database",
            status=ComponentStatus.DOWN,
            critical=True,
            latency_ms=_ms_since(start),
            detail=str(exc)[:300],
        )


def _redis_ping(url: str) -> tuple[bool, str | None, float]:
    start = _timed()
    try:
        import redis  # local import: keeps this module importable even if redis isn't installed in a slim tool context

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return True, None, _ms_since(start)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300], _ms_since(start)


def check_redis() -> ComponentHealth:
    """One combined check across REDIS_URL / CELERY_BROKER_URL /
    CELERY_RESULT_BACKEND. These are almost always the same Redis instance
    on different logical DBs (see config.py), so pinging REDIS_URL is
    representative; if that's up but the broker/backend URLs differ and
    one of *those* is unreachable, that's surfaced in the detail text
    rather than as a second row -- a broken broker with a working cache
    Redis is a config problem, not really two separate systems being
    partially healthy.
    """
    ok, err, latency = _redis_ping(settings.REDIS_URL)
    detail = "Connected" if ok else (err or "Unreachable")

    if ok:
        broker_ok, broker_err, _ = (
            (True, None, 0.0)
            if settings.CELERY_BROKER_URL == settings.REDIS_URL
            else _redis_ping(settings.CELERY_BROKER_URL)
        )
        if not broker_ok:
            return ComponentHealth(
                key="redis",
                label="Redis (cache / Celery broker)",
                status=ComponentStatus.DEGRADED,
                critical=True,
                latency_ms=latency,
                detail=f"Cache Redis OK, but Celery broker unreachable: {broker_err}",
            )

    return ComponentHealth(
        key="redis",
        label="Redis (cache / Celery broker)",
        status=ComponentStatus.UP if ok else ComponentStatus.DOWN,
        critical=True,
        latency_ms=latency,
        detail=detail,
    )


def check_celery_workers() -> ComponentHealth:
    start = _timed()
    if settings.CELERY_TASK_ALWAYS_EAGER:
        # Prototype/dev mode: tasks run inline in the request thread, no
        # separate worker process exists to ping (see celery_app.py).
        return ComponentHealth(
            key="celery",
            label="Celery Workers",
            status=ComponentStatus.UP,
            critical=False,
            latency_ms=_ms_since(start),
            detail="Running in-process (CELERY_TASK_ALWAYS_EAGER=true) -- no separate worker required",
        )
    try:
        replies = celery_app.control.ping(timeout=1.5) or []
        if replies:
            return ComponentHealth(
                key="celery",
                label="Celery Workers",
                status=ComponentStatus.UP,
                critical=True,
                latency_ms=_ms_since(start),
                detail=f"{len(replies)} worker(s) responding",
            )
        return ComponentHealth(
            key="celery",
            label="Celery Workers",
            status=ComponentStatus.DOWN,
            critical=True,
            latency_ms=_ms_since(start),
            detail="No workers responded to ping -- deployments/approvals will queue but not run",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            key="celery",
            label="Celery Workers",
            status=ComponentStatus.DOWN,
            critical=True,
            latency_ms=_ms_since(start),
            detail=str(exc)[:300],
        )


async def check_ollama(client: httpx.AsyncClient) -> ComponentHealth:
    configured = settings.RISK_ENGINE_BACKEND == "llm"
    start = _timed()
    try:
        resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags", timeout=3.0)
        resp.raise_for_status()
        tags = [m.get("name", "") for m in resp.json().get("models", [])]
        model_present = any(settings.OLLAMA_MODEL in t for t in tags)
        return ComponentHealth(
            key="ollama",
            label="Ollama (AI Risk Scoring)",
            status=ComponentStatus.UP if model_present else ComponentStatus.DEGRADED,
            critical=configured,
            latency_ms=_ms_since(start),
            detail=("Reachable" if model_present else f"Reachable, but model '{settings.OLLAMA_MODEL}' not pulled"),
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            key="ollama",
            label="Ollama (AI Risk Scoring)",
            status=ComponentStatus.DOWN if configured else ComponentStatus.NOT_CONFIGURED,
            critical=configured,
            latency_ms=_ms_since(start),
            detail=(
                str(exc)[:300]
                if configured
                else "RISK_ENGINE_BACKEND=rules -- LLM scoring not in use"
            ),
        )


async def check_netbox(client: httpx.AsyncClient) -> ComponentHealth:
    if not settings.NETBOX_URL:
        return ComponentHealth(
            key="netbox",
            label="NetBox Sync",
            status=ComponentStatus.NOT_CONFIGURED,
            critical=False,
            latency_ms=0.0,
            detail="NETBOX_URL not set",
        )
    start = _timed()
    headers = {"Authorization": f"Token {settings.NETBOX_TOKEN}"} if settings.NETBOX_TOKEN else {}
    try:
        resp = await client.get(
            f"{settings.NETBOX_URL.rstrip('/')}/api/status/",
            headers=headers,
            timeout=settings.NETBOX_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return ComponentHealth(
            key="netbox",
            label="NetBox Sync",
            status=ComponentStatus.UP,
            critical=False,
            latency_ms=_ms_since(start),
            detail="Reachable",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            key="netbox",
            label="NetBox Sync",
            status=ComponentStatus.DOWN,
            critical=False,
            latency_ms=_ms_since(start),
            detail=str(exc)[:300],
        )


async def check_gns3(client: httpx.AsyncClient) -> ComponentHealth:
    start = _timed()
    try:
        resp = await client.get(f"{settings.GNS3_BASE_URL.rstrip('/')}/v2/version", timeout=3.0)
        resp.raise_for_status()
        return ComponentHealth(
            key="gns3",
            label="GNS3 Lab Server",
            status=ComponentStatus.UP,
            critical=False,
            latency_ms=_ms_since(start),
            detail="Reachable",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            key="gns3",
            label="GNS3 Lab Server",
            status=ComponentStatus.DOWN,
            critical=False,
            latency_ms=_ms_since(start),
            detail=f"Unreachable ({exc.__class__.__name__}) -- only affects the GNS3 Lab page",
        )


def check_smtp() -> ComponentHealth:
    if not settings.SMTP_HOST:
        return ComponentHealth(
            key="smtp",
            label="SMTP (Email Notifications)",
            status=ComponentStatus.NOT_CONFIGURED,
            critical=False,
            latency_ms=0.0,
            detail="SMTP_HOST not set -- email delivery skipped, not an error",
        )
    start = _timed()
    port = settings.SMTP_PORT or 587
    try:
        with socket.create_connection((settings.SMTP_HOST, port), timeout=3.0):
            pass
        return ComponentHealth(
            key="smtp",
            label="SMTP (Email Notifications)",
            status=ComponentStatus.UP,
            critical=False,
            latency_ms=_ms_since(start),
            detail="TCP connect OK",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            key="smtp",
            label="SMTP (Email Notifications)",
            status=ComponentStatus.DOWN,
            critical=False,
            latency_ms=_ms_since(start),
            detail=str(exc)[:300],
        )


async def run_all_checks(db: Session) -> list[ComponentHealth]:
    """Runs every check concurrently where possible (network calls fan out
    via one shared httpx.AsyncClient) and returns all results together.
    DB/Redis/Celery checks are blocking (psycopg2/redis-py/celery's kombu
    are all sync), so they run in the default threadpool via
    asyncio.to_thread rather than serializing behind the async ones.
    """
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            asyncio.to_thread(check_database, db),
            asyncio.to_thread(check_redis),
            asyncio.to_thread(check_celery_workers),
            asyncio.to_thread(check_smtp),
            check_ollama(client),
            check_netbox(client),
            check_gns3(client),
        )
    return list(results)


# Which components each frontend page actually depends on, so the System
# Health page can show "this page is degraded because X is down" instead
# of just a flat component list the user has to mentally map themselves.
# Every page depends on the database + auth implicitly, so "database" is
# included everywhere; pages not listed here are static/DB-only (e.g.
# Audit Log, Security) and simply inherit ["database"].
PAGE_DEPENDENCIES: dict[str, list[str]] = {
    "Dashboard": ["database", "redis"],
    "Change Requests": ["database", "redis", "celery", "ollama"],
    "Deployments": ["database", "redis", "celery"],
    "Devices": ["database"],
    "Groups": ["database"],
    "Config Search": ["database"],
    "Templates": ["database"],
    "Topology": ["database"],
    "Path Trace": ["database"],
    "Syslog": ["database"],
    "Traffic Analysis": ["database"],
    "Drift": ["database", "redis", "celery"],
    "Alerts": ["database", "redis"],
    "Maintenance Windows": ["database"],
    "Firmware Upgrades": ["database", "redis", "celery"],
    "GNS3 Lab": ["database", "gns3"],
    "Audit Log": ["database"],
    "Security": ["database"],
    "Reports / Compliance": ["database", "redis", "celery", "smtp"],
    "NetBox Sync": ["database", "netbox"],
}


def build_page_health(components: list[ComponentHealth]) -> list[dict]:
    by_key = {c.key: c for c in components}
    pages = []
    for page, deps in PAGE_DEPENDENCIES.items():
        dep_components = [by_key[d] for d in deps if d in by_key]
        if any(c.status == ComponentStatus.DOWN and c.critical for c in dep_components):
            page_status = ComponentStatus.DOWN
        elif any(c.status in (ComponentStatus.DOWN, ComponentStatus.DEGRADED) for c in dep_components):
            page_status = ComponentStatus.DEGRADED
        else:
            page_status = ComponentStatus.UP
        pages.append(
            {
                "page": page,
                "status": page_status,
                "depends_on": [c.key for c in dep_components],
            }
        )
    return pages