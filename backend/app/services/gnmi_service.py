"""gNMI streaming telemetry (dial-in SUBSCRIBE), alongside SNMP polling.

SNMP polling (app.services.snmp_service / metrics_service) only ever
answers "what were this device's counters as of the last poll" --
SNMP_POLL_INTERVAL_SECONDS defaults to 60s, so a flap or a burst that
happens and clears between two polls is invisible, and shrinking that
interval further just means walking every SNMP-enabled device's
interface table more often. gNMI SUBSCRIBE (RFC-adjacent, OpenConfig-
modeled; supported natively by Arista EOS, Juniper Junos, and Cisco
IOS-XE/XR) flips that: the device pushes updates on its own schedule
(a `sample_interval` as low as sub-second, hardware permitting) over a
single long-lived streamed RPC, instead of NetGuard polling for them.

This module is the gNMI counterpart to flow_service.py's UDP listeners,
but dial-in rather than dial-out: instead of one shared listener socket,
one _DeviceSubscription task is supervised per gnmi-enabled Device, each
holding its own SUBSCRIBE session open for as long as the device stays
reachable and supports_gnmi stays true.

Subscribed paths are OpenConfig interface state/counters
(/interfaces/interface/state/counters/...), which is the paths every
listed vendor implements against the same YANG model -- vendor-specific
paths (Cisco native YANG, Arista EOS-native paths) are deliberately not
used here for the same reason config_intent_service normalizes on a
common shape rather than one vendor's dialect: a path list this module
understands should work unmodified against any of the three platforms
rather than needing a per-vendor branch.

Updates are buffered in memory per device and flushed to InterfaceMetric
(source="gnmi") every settings.GNMI_METRIC_FLUSH_INTERVAL_SECONDS rather
than written on every single SUBSCRIBE update -- a device sampling at
1s across dozens of interfaces would otherwise be one INSERT per
interface per second, far more write volume than the Health/Interface
pages need to render a smooth graph.

Requires the `pygnmi` package (see requirements.txt) -- optional at
import time the same way LLMScorer's `anthropic`/`ollama` calls are:
gnmi_service is only ever imported by app.main's lifespan hook when
GNMI_INPROCESS_STREAMING_ENABLED is true, and _DeviceSubscription.run
catches ImportError so a device flagged supports_gnmi=true on a build
without pygnmi installed degrades to "streaming unavailable" (surfaced
via last_gnmi_error) rather than crashing the whole app at import time.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.device import Device
from app.models.interface_metric import InterfaceMetric
from app.services import credential_service

logger = logging.getLogger("netguard.gnmi")

# OpenConfig interface counter/state leaves this module understands.
# (path, friendly_field) -- friendly_field maps onto the same shape
# InterfaceMetric already stores for SNMP-sourced rows, so the query
# layer (top-of-Health-tab charts, etc.) doesn't need to know which
# collection path produced a given row beyond the `source` column.
_SUBSCRIBE_PATHS: list[str] = [
    "/interfaces/interface/state/counters/in-octets",
    "/interfaces/interface/state/counters/out-octets",
    "/interfaces/interface/state/counters/in-errors",
    "/interfaces/interface/state/counters/out-errors",
    "/interfaces/interface/state/admin-status",
    "/interfaces/interface/state/oper-status",
]


@dataclass
class _IfaceAccumulator:
    """Latest known value per counter for one interface on one device,
    updated in place as SUBSCRIBE updates stream in and read/reset by
    the periodic flush. Mirrors the columns InterfaceMetric already has
    so the flush is a straight field copy."""

    if_descr: str
    in_octets: int | None = None
    out_octets: int | None = None
    errors: int | None = None
    speed_bps: int | None = None


def _extract_interface_name(gnmi_path) -> str | None:
    """Pulls the `name` key off the `interface[name=X]` path element --
    every leaf under /interfaces/interface/... carries it, so this is
    shared by every update regardless of which counter it's for."""
    for elem in gnmi_path.elem:
        if elem.name == "interface" and "name" in elem.key:
            return elem.key["name"]
    return None


def _leaf_name(gnmi_path) -> str | None:
    if not gnmi_path.elem:
        return None
    return gnmi_path.elem[-1].name


class _DeviceSubscription:
    """One long-lived gNMI SUBSCRIBE session for one device. Owns
    reconnect/backoff for its own device only -- a bad cert or an
    unreachable device never blocks or slows down any other device's
    session, since each runs as an independent asyncio task."""

    def __init__(self, device_id) -> None:
        self.device_id = device_id
        self._stop = asyncio.Event()
        self._accumulators: dict[str, _IfaceAccumulator] = {}
        self._lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        flush_task = asyncio.create_task(self._flush_loop())
        try:
            while not self._stop.is_set():
                try:
                    await self._connect_and_stream()
                except ImportError:
                    self._record_error("pygnmi is not installed -- see requirements.txt")
                    return  # no point retrying; the dependency isn't there
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never let one bad session crash the supervisor
                    self._record_error(str(exc))
                    logger.warning("gNMI session for device %s failed: %s", self.device_id, exc)

                if self._stop.is_set():
                    break
                await asyncio.sleep(settings.GNMI_RECONNECT_BACKOFF_SECONDS)
        finally:
            flush_task.cancel()
            await self._flush(final=True)

    def _record_error(self, message: str) -> None:
        db = SessionLocal()
        try:
            device = db.query(Device).filter(Device.id == self.device_id).first()
            if device:
                device.last_gnmi_error = message
                db.commit()
        finally:
            db.close()

    async def _connect_and_stream(self) -> None:
        # Imported lazily (not at module top) so the whole module still
        # imports cleanly -- and gnmi_service can still be unit-tested --
        # on a build where the optional pygnmi dependency isn't
        # installed; only a device actually flagged supports_gnmi=true
        # ever reaches this import.
        from pygnmi.client import gNMIclient

        db = SessionLocal()
        try:
            device = db.query(Device).filter(Device.id == self.device_id).first()
            if not device or not device.supports_gnmi:
                self._stop.set()
                return
            target = (device.ip_address, device.gnmi_port or 9339)
            username = device.gnmi_username
            password = credential_service.get_gnmi_password(device) if username else None
            sample_interval_ns = (device.gnmi_sample_interval_ms or settings.GNMI_DEFAULT_SAMPLE_INTERVAL_MS) * 1_000_000
            use_tls = device.gnmi_use_tls
            skip_verify = device.gnmi_skip_verify
        finally:
            db.close()

        subscribe_request = {
            "subscription": [{"path": p, "mode": "sample", "sample_interval": sample_interval_ns} for p in _SUBSCRIBE_PATHS],
            "mode": "stream",
            "encoding": "json_ietf",
        }

        async with gNMIclient(
            target=target,
            username=username,
            password=password,
            insecure=not use_tls,
            skip_verify=skip_verify,
        ) as client:
            self._record_error(None)  # clear any previous error now that the session is up
            async for update in client.subscribe_stream(subscribe=subscribe_request):
                if self._stop.is_set():
                    break
                await self._handle_update(update)

    async def _handle_update(self, update) -> None:
        notification = getattr(update, "update", None) or update.get("update") if isinstance(update, dict) else None
        if not notification:
            return
        for u in getattr(notification, "update", []) or notification.get("update", []):
            path = u.path if hasattr(u, "path") else u.get("path")
            value = u.val if hasattr(u, "val") else u.get("val")
            iface = _extract_interface_name(path)
            leaf = _leaf_name(path)
            if not iface or leaf is None or value is None:
                continue

            async with self._lock:
                acc = self._accumulators.setdefault(iface, _IfaceAccumulator(if_descr=iface))
                if leaf == "in-octets":
                    acc.in_octets = int(value)
                elif leaf == "out-octets":
                    acc.out_octets = int(value)
                elif leaf in ("in-errors", "out-errors"):
                    acc.errors = (acc.errors or 0) + int(value)

        db = SessionLocal()
        try:
            device = db.query(Device).filter(Device.id == self.device_id).first()
            if device:
                device.last_gnmi_update_at = datetime.datetime.now(datetime.timezone.utc)
                db.commit()
        finally:
            db.close()

    async def _flush_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(settings.GNMI_METRIC_FLUSH_INTERVAL_SECONDS)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self, final: bool = False) -> None:
        async with self._lock:
            if not self._accumulators:
                return
            snapshot = dict(self._accumulators)
            if not final:
                # Errors reset to 0 between flushes (delta-style, matching
                # InterfaceMetric.error_delta's SNMP semantics) so a
                # steady trickle of errors isn't double-counted across
                # flush windows; octets_total is cumulative and carried
                # forward untouched.
                for acc in self._accumulators.values():
                    acc.errors = None

        def _write(db: Session) -> None:
            for iface, acc in snapshot.items():
                octets_total = None
                if acc.in_octets is not None or acc.out_octets is not None:
                    octets_total = (acc.in_octets or 0) + (acc.out_octets or 0)
                db.add(
                    InterfaceMetric(
                        device_id=self.device_id,
                        if_index=iface,
                        if_descr=acc.if_descr,
                        octets_total=octets_total,
                        errors=acc.errors,
                        error_delta=acc.errors,
                        source="gnmi",
                    )
                )
            db.commit()

        db = SessionLocal()
        try:
            await asyncio.to_thread(_write, db)
        except Exception:
            logger.exception("Failed to flush gNMI metrics for device %s", self.device_id)
            db.rollback()
        finally:
            db.close()


class GnmiSupervisor:
    """Reconciles the running set of _DeviceSubscription tasks against
    Device.supports_gnmi on a poll cadence -- so flagging a device on/off
    from the UI takes effect within GNMI_DEVICE_ROSTER_REFRESH_SECONDS
    without an app restart, the same way SNMP's poll sweep already picks
    up newly-enabled devices on its own next tick."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, tuple[_DeviceSubscription, asyncio.Task]] = {}
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            self._loop_task.cancel()
        for sub, task in self._subscriptions.values():
            sub.stop()
            task.cancel()
        self._subscriptions.clear()

    async def _reconcile_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self._reconcile_once()
                await asyncio.sleep(settings.GNMI_DEVICE_ROSTER_REFRESH_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _reconcile_once(self) -> None:
        def _fetch_enabled_ids() -> list[str]:
            db = SessionLocal()
            try:
                return [str(d.id) for d in db.query(Device.id).filter(Device.supports_gnmi.is_(True)).all()]
            finally:
                db.close()

        enabled_ids = set(await asyncio.to_thread(_fetch_enabled_ids))
        current_ids = set(self._subscriptions.keys())

        for device_id in enabled_ids - current_ids:
            sub = _DeviceSubscription(device_id)
            task = asyncio.create_task(sub.run())
            self._subscriptions[device_id] = (sub, task)
            logger.info("Started gNMI SUBSCRIBE session for device %s", device_id)

        for device_id in current_ids - enabled_ids:
            sub, task = self._subscriptions.pop(device_id)
            sub.stop()
            task.cancel()
            logger.info("Stopped gNMI SUBSCRIBE session for device %s (supports_gnmi disabled)", device_id)

    def status(self) -> dict[str, bool]:
        """device_id -> whether a subscription task is currently running.
        Used by the /gnmi/status API for a lightweight "is this actually
        streaming" check without querying InterfaceMetric."""
        return {device_id: not task.done() for device_id, (_sub, task) in self._subscriptions.items()}


_supervisor: GnmiSupervisor | None = None


async def start_gnmi_supervisor() -> GnmiSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = GnmiSupervisor()
        await _supervisor.start()
    return _supervisor


async def stop_gnmi_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        await _supervisor.stop()
        _supervisor = None


def get_supervisor() -> GnmiSupervisor | None:
    return _supervisor
