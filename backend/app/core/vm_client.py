"""VictoriaMetrics client -- time-series store backing the SNMP Health
Dashboard (device_metrics/interface_metrics), replacing what used to be
two Postgres tables (see app.models.device_metric / interface_metric,
now retired).

VictoriaMetrics speaks the Prometheus remote-write/HTTP-API protocol:
writes go through its native JSON-lines `/api/v1/import` endpoint (chosen
over the Prometheus exposition-text `/api/v1/import/prometheus` variant
because it carries explicit millisecond timestamps per sample, so a
poll's samples land at the poll's actual time rather than "now" -- useful
for backfill and for keeping every field from one poll_device() call
under one exact timestamp so metric_history can zip per-metric range
queries back into one row per poll); reads go through the standard
PromQL `/api/v1/query` (instant) and `/api/v1/query_range` endpoints,
which VictoriaMetrics implements 1:1 with Prometheus.

This module is deliberately domain-aware (metric-name constants, the
device/interface schema) rather than a generic TSDB wrapper -- there's
only one consumer (metrics_service), and keeping the "what do we call
these series" decisions in one place is more useful here than a thin,
metric-name-agnostic client would be.
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Iterable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# --- Device-level gauges (one row -> one sample per field, all sharing the
# poll's timestamp) -----------------------------------------------------
DEVICE_METRIC_FIELDS = (
    "cpu_utilization_pct",
    "memory_utilization_pct",
    "interface_utilization_pct",
    "interface_errors",
    "temperature_celsius",
    "uptime_seconds",
    "health_score",
    "interface_octets_total",
    "interface_speed_bps",
)
_DEVICE_METRIC_NAME = "netguard_device_{field}".format

# fan_status / power_supply_status / health_color are enum-like strings, not
# gauges -- encoded as a constant-1 sample carrying the string as a label
# (the standard Prometheus "info metric" pattern), so PromQL can still
# select/group on them, e.g. netguard_device_health_color{color="red"}.
DEVICE_INFO_FIELDS = {
    "fan_status": "netguard_device_fan_status",
    "power_supply_status": "netguard_device_power_status",
    "health_color": "netguard_device_health_color",
}

# --- Per-interface gauges ------------------------------------------------
INTERFACE_METRIC_FIELDS = (
    "octets_total",
    "speed_bps",
    "errors",
    "utilization_pct",
    "error_delta",
)
_INTERFACE_METRIC_NAME = "netguard_interface_{field}".format

# How far back "latest value" lookups are allowed to reach. Generous on
# purpose (matches the old unbounded "ORDER BY polled_at DESC LIMIT 1"
# behavior) -- a device that hasn't polled in days should still show its
# last known reading rather than silently going "unknown", the fleet
# health rollup's own staleness signal (has_stale_metric) is what flags
# that case, not a query-side cutoff.
_LATEST_LOOKBACK = "45d"

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=settings.VICTORIAMETRICS_URL, timeout=10.0)
    return _client


def _to_ms(ts: datetime.datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return int(ts.timestamp() * 1000)


def _from_ms(ms: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)


def _label_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _write_samples(samples: Iterable[tuple[str, dict[str, str], float, datetime.datetime]]) -> None:
    """Best-effort batch write. A VM hiccup must never break an SNMP poll
    or take down the Celery task that called it -- same policy as
    event_bus.publish_event and notification_service.
    """
    samples = list(samples)
    if not samples:
        return
    lines = []
    for metric_name, labels, value, timestamp in samples:
        metric = {"__name__": metric_name, **{k: str(v) for k, v in labels.items()}}
        lines.append({"metric": metric, "values": [value], "timestamps": [_to_ms(timestamp)]})
    body = "\n".join(json.dumps(line) for line in lines)
    try:
        resp = _get_client().post(
            "/api/v1/import", content=body, headers={"Content-Type": "application/x-ndjson"}
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 - writes must never break the caller
        logger.warning("vm_client: write of %d sample(s) failed", len(lines), exc_info=True)


def write_device_poll(device_id: uuid.UUID, hostname: str, timestamp: datetime.datetime, values: dict) -> None:
    """One device-level poll's worth of gauges + info metrics, all stamped
    with the same timestamp so metric_history can zip per-field range
    queries back into single rows."""
    labels = {"device_id": str(device_id), "hostname": hostname}
    samples = []
    for field in DEVICE_METRIC_FIELDS:
        value = values.get(field)
        if value is not None:
            samples.append((_DEVICE_METRIC_NAME(field=field), labels, float(value), timestamp))
    for field, metric_name in DEVICE_INFO_FIELDS.items():
        value = values.get(field)
        if value is not None:
            samples.append((metric_name, {**labels, "value": str(value)}, 1.0, timestamp))
    _write_samples(samples)


def write_interface_poll(
    device_id: uuid.UUID, hostname: str, if_index: str, if_descr: str, timestamp: datetime.datetime, values: dict
) -> None:
    labels = {"device_id": str(device_id), "hostname": hostname, "if_index": if_index, "if_descr": if_descr}
    samples = []
    for field in INTERFACE_METRIC_FIELDS:
        value = values.get(field)
        if value is not None:
            samples.append((_INTERFACE_METRIC_NAME(field=field), labels, float(value), timestamp))
    _write_samples(samples)


def write_interface_polls(
    device_id: uuid.UUID, hostname: str, timestamp: datetime.datetime, entries: list[dict]
) -> None:
    """Batched counterpart of write_interface_poll -- one HTTP round trip
    for an entire poll's interfaces instead of one per interface, which
    matters for switches with dozens/hundreds of ports. Each entry needs
    if_index, if_descr, and whichever INTERFACE_METRIC_FIELDS it has."""
    samples = []
    for entry in entries:
        labels = {
            "device_id": str(device_id),
            "hostname": hostname,
            "if_index": entry["if_index"],
            "if_descr": entry.get("if_descr") or f"if{entry['if_index']}",
        }
        for field in INTERFACE_METRIC_FIELDS:
            value = entry.get(field)
            if value is not None:
                samples.append((_INTERFACE_METRIC_NAME(field=field), labels, float(value), timestamp))
    _write_samples(samples)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _instant_query(promql: str) -> list[dict]:
    try:
        resp = _get_client().get("/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])
    except Exception:  # noqa: BLE001 - a query failure should read as "no data", not crash the page
        logger.warning("vm_client: instant query failed: %s", promql, exc_info=True)
        return []


def _range_query(promql: str, start: datetime.datetime, end: datetime.datetime, step_seconds: int) -> list[dict]:
    try:
        resp = _get_client().get(
            "/api/v1/query_range",
            params={
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": f"{max(step_seconds, 1)}s",
            },
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])
    except Exception:  # noqa: BLE001
        logger.warning("vm_client: range query failed: %s", promql, exc_info=True)
        return []


DEVICE_INT_FIELDS = {"interface_errors", "uptime_seconds", "health_score"}
INTERFACE_INT_FIELDS = {"octets_total", "speed_bps", "errors", "error_delta"}


def _cast_int_fields(row: dict, int_fields: set[str]) -> None:
    for field in int_fields:
        if row.get(field) is not None:
            row[field] = int(row[field])


def latest_device_metrics(device_id: uuid.UUID) -> dict | None:
    """Most recent value of every device-level gauge + info field for one
    device, merged into a single dict keyed by field name (mirrors the old
    DeviceMetric row's columns). Returns None if nothing has ever been
    written for this device."""
    promql = f'last_over_time({{__name__=~"netguard_device_.*",device_id="{_label_escape(str(device_id))}"}}[{_LATEST_LOOKBACK}])'
    results = _instant_query(promql)
    if not results:
        return None

    row: dict = {"device_id": device_id, "polled_at": None}
    # Per-field timestamp of whatever's currently in `row[field]`, so a
    # later series in the response never overwrites an already-newer
    # value for that same field -- see field_ts's docstring note below
    # for why this matters specifically for the info fields.
    field_ts: dict[str, datetime.datetime] = {}
    latest_ts = None
    for series in results:
        name = series["metric"].get("__name__", "")
        ts, value_str = series["value"]
        ts_dt = _from_ms(float(ts) * 1000)
        if latest_ts is None or ts_dt > latest_ts:
            latest_ts = ts_dt

        matched_field = None
        value = None
        for field in DEVICE_METRIC_FIELDS:
            if name == _DEVICE_METRIC_NAME(field=field):
                matched_field, value = field, float(value_str)
                break
        else:
            for field, metric_name in DEVICE_INFO_FIELDS.items():
                if name == metric_name:
                    matched_field, value = field, series["metric"].get("value")
                    break
        if matched_field is None:
            continue
        # Info fields (fan_status/power_supply_status/health_color) are
        # written as a constant-1 sample with the *value itself* as a
        # label (see DEVICE_INFO_FIELDS's comment) -- every time the
        # value changes, that's a brand new, distinct Prometheus series,
        # and the old one doesn't vanish; it keeps satisfying
        # last_over_time() for the rest of _LATEST_LOOKBACK. Without this
        # per-field freshness check, whichever stale series happened to
        # land last in this (arbitrarily ordered) API response would
        # silently win -- e.g. a device that was briefly "red" hours ago
        # and is "green" right now could still read back as "red"
        # depending on response ordering. Plain gauges only ever have one
        # live series per device, so this check is a no-op for them.
        if matched_field in field_ts and field_ts[matched_field] >= ts_dt:
            continue
        field_ts[matched_field] = ts_dt
        row[matched_field] = value
    row["polled_at"] = latest_ts or datetime.datetime.now(datetime.timezone.utc)
    _cast_int_fields(row, DEVICE_INT_FIELDS)
    row["id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"netguard-metric:{device_id}:{int(row['polled_at'].timestamp() * 1000)}")
    return row


def fleet_latest_health() -> dict[uuid.UUID, dict]:
    """One query for the whole fleet's latest health_score + health_color
    (avoids an N-device query loop in fleet_health_summary) -> {device_id:
    {"health_score": ..., "health_color": ...}}."""
    out: dict[uuid.UUID, dict] = {}
    ts_by_device: dict[uuid.UUID, datetime.datetime] = {}
    for series in _instant_query(f"last_over_time(netguard_device_health_score[{_LATEST_LOOKBACK}])"):
        dev_id = series["metric"].get("device_id")
        if not dev_id:
            continue
        _, value_str = series["value"]
        out.setdefault(uuid.UUID(dev_id), {})["health_score"] = float(value_str)
    # health_color is an info metric (see DEVICE_INFO_FIELDS) -- every
    # past color value for a device is its own still-live Prometheus
    # series within the lookback window, so this query can return
    # several results per device_id. Keep only the one with the newest
    # sample timestamp per device, same fix as latest_device_metrics,
    # or a stale color can silently win depending on response ordering.
    for series in _instant_query(f"last_over_time(netguard_device_health_color[{_LATEST_LOOKBACK}])"):
        dev_id_str = series["metric"].get("device_id")
        if not dev_id_str:
            continue
        dev_id = uuid.UUID(dev_id_str)
        ts, value_str = series["value"]
        ts_dt = _from_ms(float(ts) * 1000)
        if dev_id in ts_by_device and ts_by_device[dev_id] >= ts_dt:
            continue
        ts_by_device[dev_id] = ts_dt
        out.setdefault(dev_id, {})["health_color"] = series["metric"].get("value")
    return out


def fleet_latest_metrics() -> dict[uuid.UUID, dict]:
    """One query for the whole fleet's latest full sample (every device
    gauge + info field, not just health_score/health_color) -> {device_id:
    row-dict}, keyed the same way as latest_device_metrics()'s return
    value. Backs the dashboard's Top CPU/Memory/Bandwidth widgets,
    uplink rollup, reboot list, and interface-error list, all of which
    previously ran off one Postgres "latest DeviceMetric row per device"
    join -- this is the fleet-wide counterpart of that join, done as two
    PromQL queries (device gauges + device info labels) instead of one
    per-device HTTP round trip.
    """
    rows: dict[uuid.UUID, dict] = {}
    # Per (device_id, field) timestamp of whatever's currently in that
    # row's field -- see latest_device_metrics for why this is needed:
    # info fields (fan_status/power_supply_status/health_color) leave
    # every past value as its own still-live series within the lookback
    # window, so without this a stale reading can win purely on response
    # ordering.
    field_ts: dict[tuple[uuid.UUID, str], datetime.datetime] = {}

    for series in _instant_query(f'last_over_time({{__name__=~"netguard_device_.*"}}[{_LATEST_LOOKBACK}])'):
        labels = series["metric"]
        dev_id_str = labels.get("device_id")
        if not dev_id_str:
            continue
        try:
            dev_id = uuid.UUID(dev_id_str)
        except ValueError:
            continue
        row = rows.setdefault(dev_id, {"device_id": dev_id, "hostname": labels.get("hostname"), "polled_at": None})
        name = labels.get("__name__", "")
        ts, value_str = series["value"]
        ts_dt = _from_ms(float(ts) * 1000)
        if row["polled_at"] is None or ts_dt > row["polled_at"]:
            row["polled_at"] = ts_dt

        matched_field = None
        value = None
        for field in DEVICE_METRIC_FIELDS:
            if name == _DEVICE_METRIC_NAME(field=field):
                matched_field, value = field, float(value_str)
                break
        else:
            for field, metric_name in DEVICE_INFO_FIELDS.items():
                if name == metric_name:
                    matched_field, value = field, labels.get("value")
                    break
        if matched_field is None:
            continue
        key = (dev_id, matched_field)
        if key in field_ts and field_ts[key] >= ts_dt:
            continue
        field_ts[key] = ts_dt
        row[matched_field] = value

    for row in rows.values():
        _cast_int_fields(row, DEVICE_INT_FIELDS)
    return rows


def fleet_metric_history_hourly(field: str, start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """Fleet-wide hourly-bucketed average of one device gauge (e.g.
    "cpu_utilization_pct") across every device with a reading in each
    hour -- the dashboard's 24h fleet-health trend graph. A plain PromQL
    `avg(...)` range query with a 1h step does the bucketing/averaging
    server-side (VictoriaMetrics samples the aggregated series once per
    step), which is simpler and cheaper than pulling every device's raw
    samples and averaging them in Python.
    """
    promql = f"avg({_DEVICE_METRIC_NAME(field=field)})"
    out = []
    for series in _range_query(promql, start, end, step_seconds=3600):
        for ts, value_str in series.get("values", []):
            out.append({"timestamp": _from_ms(float(ts) * 1000), "value": float(value_str)})
    return out


def latest_interface_metrics(device_id: uuid.UUID) -> list[dict]:
    """Latest reading per (if_index) for one device, merged across all
    netguard_interface_* series -- the per-link counterpart of
    latest_device_metrics."""
    promql = f'last_over_time({{__name__=~"netguard_interface_.*",device_id="{_label_escape(str(device_id))}"}}[{_LATEST_LOOKBACK}])'
    by_index: dict[str, dict] = {}
    for series in _instant_query(promql):
        labels = series["metric"]
        if_index = labels.get("if_index")
        if not if_index:
            continue
        row = by_index.setdefault(
            if_index,
            {"if_index": if_index, "if_descr": labels.get("if_descr", f"if{if_index}"), "polled_at": None},
        )
        name = labels.get("__name__", "")
        ts, value_str = series["value"]
        ts_dt = _from_ms(float(ts) * 1000)
        if row["polled_at"] is None or ts_dt > row["polled_at"]:
            row["polled_at"] = ts_dt
        for field in INTERFACE_METRIC_FIELDS:
            if name == _INTERFACE_METRIC_NAME(field=field):
                row[field] = float(value_str)
                break
    rows = list(by_index.values())
    for row in rows:
        _cast_int_fields(row, INTERFACE_INT_FIELDS)
    rows.sort(key=lambda r: r["if_descr"])
    return rows


def device_metric_history(
    device_id: uuid.UUID, start: datetime.datetime, end: datetime.datetime, step_seconds: int
) -> list[dict]:
    """Per-field range queries for one device, zipped back into one row
    per timestamp (mirrors the old chronological DeviceMetric row list).
    All fields share the same poll timestamp at write time, and share the
    same `step` here, so results line up index-for-index in practice;
    zipping by timestamp (rather than by index) is still used below to
    stay correct if VictoriaMetrics ever returns partially-populated
    steps for one field but not another (e.g. a field that started being
    polled partway through the window).
    """
    rows_by_ts: dict[int, dict] = {}
    for field in DEVICE_METRIC_FIELDS:
        promql = f'{_DEVICE_METRIC_NAME(field=field)}{{device_id="{_label_escape(str(device_id))}"}}'
        for series in _range_query(promql, start, end, step_seconds):
            for ts, value_str in series.get("values", []):
                ts_ms = int(float(ts) * 1000)
                row = rows_by_ts.setdefault(ts_ms, {"device_id": device_id, "polled_at": _from_ms(ts_ms)})
                row[field] = float(value_str)

    ordered_ts = sorted(rows_by_ts)
    rows = []
    for ts_ms in ordered_ts:
        row = rows_by_ts[ts_ms]
        # Deterministic id from (device_id, timestamp): callers/schemas
        # still expect a stable `id` field (React list keys, etc.) even
        # though a VM sample has no row identity of its own.
        row["id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"netguard-metric:{device_id}:{ts_ms}")
        _cast_int_fields(row, DEVICE_INT_FIELDS)
        rows.append(row)
    return rows


def interface_metric_history(
    device_id: uuid.UUID, if_descr: str, start: datetime.datetime, end: datetime.datetime, step_seconds: int
) -> list[dict]:
    """Same format as device_metric_history but scoped to a single interface."""
    rows_by_ts: dict[int, dict] = {}
    for field in INTERFACE_METRIC_FIELDS:
        promql = f'{_INTERFACE_METRIC_NAME(field=field)}{{device_id="{_label_escape(str(device_id))}", if_descr="{_label_escape(if_descr)}" }}'
        for series in _range_query(promql, start, end, step_seconds):
            for ts, value_str in series.get("values", []):
                ts_ms = int(float(ts) * 1000)
                row = rows_by_ts.setdefault(ts_ms, {"device_id": device_id, "if_descr": if_descr, "polled_at": _from_ms(ts_ms)})
                row[field] = float(value_str)

    ordered_ts = sorted(rows_by_ts)
    rows = []
    for ts_ms in ordered_ts:
        row = rows_by_ts[ts_ms]
        row["id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"netguard-ifmetric:{device_id}:{if_descr}:{ts_ms}")
        _cast_int_fields(row, INTERFACE_INT_FIELDS)
        rows.append(row)
    return rows


def delete_device_series(device_id: uuid.UUID) -> None:
    """Deletes every stored series for a device (device-level + interface)
    -- called when a device is removed, mirroring the old
    `db.query(DeviceMetric).filter(...).delete()` cleanup."""
    match = f'{{device_id="{_label_escape(str(device_id))}"}}'
    try:
        resp = _get_client().post("/api/v1/admin/tsdb/delete_series", params={"match[]": match})
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 - best-effort cleanup, don't block device deletion
        logger.warning("vm_client: delete_series failed for device %s", device_id, exc_info=True)
