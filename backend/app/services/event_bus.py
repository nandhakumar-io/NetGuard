"""Dashboard event bus (SRS 6.9 Live Deployment Dashboard).

Deployment pipelines run inside Celery worker processes, not the FastAPI
process that serves the `/dashboard/ws` WebSocket. To push updates the
instant something actually changes -- rather than the websocket endpoint
polling the DB on a fixed interval regardless of activity -- workers
publish a lightweight "something changed" event to a Redis pub/sub
channel, and every connected websocket (in every FastAPI process/replica)
subscribes to that same channel and re-renders on receipt.

Redis is already a hard dependency here (Celery broker/result backend),
so this adds no new infrastructure.
"""
import json

import redis
import redis.asyncio as aredis

from app.core.config import settings

DASHBOARD_CHANNEL = "netguard:dashboard:events"

# Separate sync client for publishers running inside Celery workers
# (regular, non-async code), and an async client for the FastAPI side
# that owns the websocket's subscribe loop.
_sync_client: redis.Redis | None = None


def _get_sync_client() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _sync_client


def publish_event(event_type: str, **payload) -> None:
    """Publish a "something changed" event. Best-effort: a Redis hiccup
    here should never take down a deployment pipeline, so failures are
    logged and swallowed, same policy as notification_service.
    """
    try:
        client = _get_sync_client()
        message = json.dumps({"type": event_type, **payload})
        client.publish(DASHBOARD_CHANNEL, message)
    except Exception:  # noqa: BLE001 - publishing must never break the pipeline
        pass


def get_async_client() -> aredis.Redis:
    """Fresh async Redis client for a websocket connection's subscribe loop."""
    return aredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
