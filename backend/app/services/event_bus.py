"""Dashboard event bus (SRS 6.9 Live Deployment Dashboard).

Deployment pipelines run inside Celery worker processes, not the FastAPI
process that serves the `/dashboard/ws`, `/topology/ws`, `/alerts/ws` and
`/notifications/ws` WebSockets. To push updates the instant something
actually changes -- rather than each websocket endpoint polling the DB on
a fixed interval regardless of activity -- workers publish a lightweight
"something changed" event to a subject on NATS, and every connected
websocket (in every FastAPI process/replica) subscribes to that same
subject and re-renders on receipt.

Backed by NATS JetStream rather than plain core NATS so that a worker
publishing an event before any websocket has subscribed (e.g. right after
a cold deploy/restart) still lands durably on the stream instead of being
silently dropped -- a newly-connecting websocket picks up from "now"
(DeliverPolicy.NEW), so it never replays a backlog of stale UI-refresh
pings, but it also never races a publish that happens a few milliseconds
before `subscribe()` completes.

Two connection strategies, matching the two calling contexts:

* `publish_event()` is called from arbitrary sync code -- Celery worker
  tasks and sync FastAPI request handlers. It hands off to a single
  background thread that owns one persistent asyncio event loop and one
  persistent JetStream connection for the whole process, so a hot path
  (e.g. a bulk change-request approval firing dozens of events) doesn't
  pay a NATS handshake per call. Best-effort: publish failures are logged
  and swallowed, same policy as notification_service -- a NATS hiccup
  must never take down a deployment pipeline.
* `get_async_client()` is called from the async websocket endpoints,
  which already run inside FastAPI's own event loop. It returns a small
  adapter exposing the same `.pubsub()` / `.subscribe()` / `.get_message()`
  / `.unsubscribe()` / `.close()` shape as the old `redis.asyncio` client,
  so none of the four websocket endpoints needed to change.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading

import nats
from nats.js.api import DeliverPolicy, RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError as JsNotFoundError

from app.core.config import settings

logger = logging.getLogger(__name__)

DASHBOARD_CHANNEL = "netguard.dashboard.events"
ALERTS_CHANNEL = "netguard.alerts.events"
NOTIFICATIONS_CHANNEL = "netguard.notifications.events"
# Topology page (node status/health colors, edges, DC/rack placement).
# Separate from DASHBOARD_CHANNEL so a busy dashboard (deployments,
# alerts) doesn't force every open Topology tab to re-render, and vice
# versa -- the two pages redraw completely different things.
TOPOLOGY_CHANNEL = "netguard.topology.events"

STREAM_NAME = "NETGUARD_EVENTS"
STREAM_SUBJECTS = ["netguard.>"]


# ---------------------------------------------------------------------------
# Publisher: one persistent background loop/connection for the whole process
# ---------------------------------------------------------------------------
class _PublisherLoop:
    """Owns a dedicated thread running its own asyncio event loop, plus a
    single persistent JetStream connection on that loop. `publish()` can be
    called from any thread (Celery worker, sync request handler) and simply
    schedules the actual publish onto this loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._nc = None
        self._js = None
        self._start_lock = threading.Lock()
        self._ready = threading.Event()

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is not None:
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, name="netguard-event-bus-publisher", daemon=True
            )
            self._thread.start()
            # Bound wait so a NATS outage at startup can't hang the caller
            # forever -- publish() below still no-ops safely if this times out.
            self._ready.wait(timeout=5)

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())
        self._ready.set()
        self._loop.run_forever()

    async def _connect(self) -> None:
        try:
            self._nc = await nats.connect(
                servers=[settings.NATS_URL],
                name="netguard-event-bus-publisher",
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
                user=settings.NATS_API_USER,
                password=settings.NATS_API_PASSWORD,
            )
            self._js = self._nc.jetstream()
            try:
                await self._js.stream_info(STREAM_NAME)
            except JsNotFoundError:
                await self._js.add_stream(
                    StreamConfig(
                        name=STREAM_NAME,
                        subjects=STREAM_SUBJECTS,
                        retention=RetentionPolicy.LIMITS,
                        max_age=60 * 60,  # 1h: these are UI-refresh pings, not an audit log
                        max_msgs=100_000,
                    )
                )
        except Exception:  # noqa: BLE001 - connect failures must not crash the thread
            logger.exception("event_bus: failed to connect to NATS at %s", settings.NATS_URL)
            self._nc = None
            self._js = None

    async def _publish(self, subject: str, payload: bytes) -> None:
        if self._js is None:
            # First publish after a failed initial connect -- retry once,
            # cheaply, rather than staying broken for the process lifetime.
            await self._connect()
        if self._js is None:
            return
        try:
            await self._js.publish(subject, payload)
        except Exception:  # noqa: BLE001 - publishing must never break the caller
            logger.warning("event_bus: publish to %s failed", subject, exc_info=True)

    def publish(self, subject: str, payload: bytes) -> None:
        self._ensure_started()
        if self._loop is None or not self._loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._publish(subject, payload), self._loop)
        except Exception:  # noqa: BLE001 - scheduling failure is still best-effort
            logger.warning("event_bus: could not schedule publish to %s", subject, exc_info=True)


_publisher = _PublisherLoop()


def publish_event(event_type: str, *, channel: str = DASHBOARD_CHANNEL, **payload) -> None:
    """Publish a "something changed" event. Best-effort: a NATS hiccup here
    should never take down a deployment pipeline, so failures are logged
    and swallowed, same policy as notification_service.
    """
    try:
        message = json.dumps({"type": event_type, **payload}).encode()
        _publisher.publish(channel, message)
    except Exception:  # noqa: BLE001 - publishing must never break the pipeline
        pass


# ---------------------------------------------------------------------------
# Subscriber: async adapter for the FastAPI websocket endpoints
# ---------------------------------------------------------------------------
class _NatsPubSubAdapter:
    """Mimics the subset of redis.asyncio's PubSub interface the websocket
    endpoints use (`subscribe`, `get_message`, `unsubscribe`, `close`), so
    dashboard.py / topology.py / alerts.py / notification.py didn't need to
    change when the event bus moved off Redis.
    """

    def __init__(self, connection: "_NatsConnection") -> None:
        self._connection = connection
        self._subscription = None
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, subject: str) -> None:
        js = await self._connection._ensure_jetstream()

        async def _handler(msg) -> None:
            await self._queue.put(msg.data)
            with contextlib.suppress(Exception):
                await msg.ack()

        # Ordered ephemeral consumer, delivering only events published from
        # now on -- a freshly-opened tab shouldn't replay an hour of stale
        # "something changed" pings, it just wants the next one.
        self._subscription = await js.subscribe(
            subject,
            ordered_consumer=True,
            deliver_policy=DeliverPolicy.NEW,
            cb=_handler,
        )

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None):
        try:
            if timeout:
                data = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            else:
                data = await self._queue.get()
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "data": data.decode()}

    async def unsubscribe(self, subject: str | None = None) -> None:
        if self._subscription is not None:
            with contextlib.suppress(Exception):
                await self._subscription.unsubscribe()
            self._subscription = None

    async def close(self) -> None:
        # No-op: the underlying connection is closed via _NatsConnection.close(),
        # matching the old code's `pubsub.close()` / `redis_client.close()` pair.
        pass


class _NatsConnection:
    """Per-websocket-connection NATS connection, mirroring how the old code
    created a fresh `aredis.Redis` client per websocket connection."""

    def __init__(self) -> None:
        self._nc = None
        self._js = None

    async def _ensure_jetstream(self):
        if self._nc is None or self._nc.is_closed:
            self._nc = await nats.connect(
                servers=[settings.NATS_URL],
                user=settings.NATS_API_USER,
                password=settings.NATS_API_PASSWORD,
            )
            self._js = self._nc.jetstream()
        return self._js

    def pubsub(self) -> _NatsPubSubAdapter:
        return _NatsPubSubAdapter(self)

    async def close(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            with contextlib.suppress(Exception):
                await self._nc.close()


def get_async_client() -> _NatsConnection:
    """Fresh NATS/JetStream connection for a websocket connection's
    subscribe loop."""
    return _NatsConnection()
