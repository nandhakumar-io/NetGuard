import asyncio
import contextlib
import datetime
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import vm_client
from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user, get_current_user_ws
from app.models.alert import Alert, AlertSeverity
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.dashboard_preference import DashboardPreference
from app.models.deployment import Deployment, DeploymentStatus
from app.models.device import Device, DeviceStatus
from app.models.interface_status import InterfaceStatus
from app.models.protocol_operation import ProtocolOperation
from app.models.snapshot import ConfigSnapshot
from app.models.subnet import Subnet, SubnetScannedHost
from app.models.user import User
from app.schemas.dashboard_preference import (
    DashboardLayoutEntry,
    DashboardPreferenceRead,
    DashboardPreferenceUpdate,
    DashboardWidgetInfo,
)
from app.services import (
    dashboard_widgets,
    event_bus,
    flow_service,
    ipam_service,
    syslog_service,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30


SPARKLINE_LOOKBACK_HOURS = 1
SPARKLINE_MAX_POINTS = 20


def _metric_sparkline(db: Session, device_id, column_name: str) -> list[float]:
    """Last-hour trend (oldest-first) for one device/metric column, for
    the Top CPU/Memory widget sparklines -- distinct from the full
    metrics_service.metric_history used by the per-device detail chart,
    since this only needs a handful of points per Top-N row, not a
    complete history payload, and runs once per row on every dashboard
    summary refresh.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=SPARKLINE_LOOKBACK_HOURS)
    end = datetime.datetime.now(datetime.timezone.utc)
    step = max(1, (SPARKLINE_LOOKBACK_HOURS * 3600) // SPARKLINE_MAX_POINTS)
    history = vm_client.device_metric_history(device_id, since, end, step_seconds=step)
    values = [row.get(column_name, 0) for row in history if row.get(column_name) is not None]
    return values[-SPARKLINE_MAX_POINTS:]


# --- Uplink Availability % -------------------------------------------------
#
# Rolls the existing "Uplinks & WAN Links" widget (a top-10-by-utilization
# list) up into a single headline stat: how many of the fleet's is_uplink
# devices are up *right now*, plus a trailing-window uptime percentage
# derived from "Interface Down: <ifDescr>" Alert rows (see
# metrics_service._sync_interface_status) on those devices -- an alert's
# created_at -> resolved_at span *is* a downtime interval, since
# alert_service.auto_resolve only clears it on a genuine down->up
# recovery. Intervals are merged per device before summing so two
# overlapping down interfaces on the same uplink device (e.g. both legs
# of a bonded WAN link dropping together) don't double-count downtime.
UPLINK_AVAILABILITY_WINDOW_DAYS = 30


def _uplink_availability(db: Session) -> dict:
    uplink_devices = db.query(Device).filter(Device.is_uplink.is_(True)).all()
    uplinks_total = len(uplink_devices)
    if uplinks_total == 0:
        return {
            "uplinks_total": 0,
            "uplinks_up": 0,
            "uptime_pct": None,
            "window_days": UPLINK_AVAILABILITY_WINDOW_DAYS,
        }

    uplinks_up = sum(1 for d in uplink_devices if d.status == DeviceStatus.ONLINE)

    now = datetime.datetime.now(datetime.timezone.utc)
    window_start = now - datetime.timedelta(days=UPLINK_AVAILABILITY_WINDOW_DAYS)
    ids = [d.id for d in uplink_devices]

    # Any "Interface Down" alert on an uplink device that was active at
    # any point during the window -- still-unresolved ones (resolved_at
    # IS NULL) count as down through `now`.
    alerts = (
        db.query(Alert)
        .filter(
            Alert.device_id.in_(ids),
            Alert.category.like("Interface Down:%"),
            (Alert.resolved_at.is_(None)) | (Alert.resolved_at >= window_start),
            Alert.created_at <= now,
        )
        .all()
    )

    intervals_by_device: dict = {}
    for a in alerts:
        start = max(a.created_at, window_start) if a.created_at else window_start
        end = min(a.resolved_at, now) if a.resolved_at else now
        if end <= start:
            continue
        intervals_by_device.setdefault(a.device_id, []).append((start, end))

    window_seconds = (now - window_start).total_seconds()
    total_downtime_seconds = 0.0
    for _device_id, intervals in intervals_by_device.items():
        intervals.sort(key=lambda t: t[0])
        merged: list[tuple] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        total_downtime_seconds += sum((e - s).total_seconds() for s, e in merged)

    possible_seconds = uplinks_total * window_seconds
    uptime_pct = round(max(0.0, (possible_seconds - total_downtime_seconds) / possible_seconds) * 100, 2) if possible_seconds else 100.0

    return {
        "uplinks_total": uplinks_total,
        "uplinks_up": uplinks_up,
        "uptime_pct": uptime_pct,
        "window_days": UPLINK_AVAILABILITY_WINDOW_DAYS,
    }


# --- IPAM Utilization Overview + Fingerprint Coverage -----------------------
#
# Cross-subnet rollup on top of app.services.ipam_service.subnet_utilization
# (per-subnet) -- surfaces the handful of subnets actually worth an
# operator's attention (near exhaustion, or never scanned) without having
# to open every subnet in IPAM one at a time. Fingerprint coverage is a
# similarly cheap aggregate over SubnetScannedHost now that OS/device-type
# fingerprinting exists, meant to nudge adoption of that feature by making
# "how much of what's actually on the wire do we have visibility into"
# visible without digging into IPAM.
IPAM_NEAR_EXHAUSTION_THRESHOLD_PCT = 85.0
IPAM_NEAR_EXHAUSTION_TOP_N = 5

# _compute_summary runs on every dashboard heartbeat (every
# HEARTBEAT_INTERVAL_SECONDS, currently 30s) via the /dashboard/live
# websocket, but subnet_utilization() is not cheap -- it decrypts every
# device's latest config snapshot per subnet to find interface IPs.
# Recomputing that every 30s for every connected dashboard would be a
# real cost for a fleet of any size, for a number that only meaningfully
# changes on the timescale of new subnets/devices/scans being added, not
# every poll cycle -- so this is cached process-wide with a short TTL
# rather than tied to any specific request or websocket connection.
_IPAM_OVERVIEW_CACHE_TTL_SECONDS = 300
_ipam_overview_cache: dict = {"computed_at": None, "value": None}


def _ipam_overview_cached(db: Session) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    cached_at = _ipam_overview_cache["computed_at"]
    if cached_at is not None and (now - cached_at).total_seconds() < _IPAM_OVERVIEW_CACHE_TTL_SECONDS:
        return _ipam_overview_cache["value"]
    value = _ipam_overview(db)
    _ipam_overview_cache["computed_at"] = now
    _ipam_overview_cache["value"] = value
    return value


def _ipam_overview(db: Session) -> dict:
    subnets = db.query(Subnet).all()
    near_exhaustion = []
    never_scanned_count = 0
    for s in subnets:
        util = ipam_service.subnet_utilization(db, s)
        if util["utilization_pct"] >= IPAM_NEAR_EXHAUSTION_THRESHOLD_PCT:
            near_exhaustion.append(
                {
                    "subnet_id": str(s.id),
                    "cidr": s.cidr,
                    "name": s.name,
                    "utilization_pct": util["utilization_pct"],
                }
            )
        if not s.last_scanned_at:
            never_scanned_count += 1
    near_exhaustion.sort(key=lambda r: r["utilization_pct"], reverse=True)

    total_live_hosts = db.query(SubnetScannedHost).count()
    fingerprinted_hosts = db.query(SubnetScannedHost).filter(SubnetScannedHost.os_guess.isnot(None)).count()

    return {
        "total_subnets": len(subnets),
        "never_scanned_count": never_scanned_count,
        "near_exhaustion_count": len(near_exhaustion),
        "near_exhaustion": near_exhaustion[:IPAM_NEAR_EXHAUSTION_TOP_N],
        "fingerprint_coverage": {
            "identified": fingerprinted_hosts,
            "total_live_hosts": total_live_hosts,
        },
    }


def _compute_summary(db: Session) -> dict:
    devices_online = db.query(Device).filter(Device.status == DeviceStatus.ONLINE).count()
    devices_total = db.query(Device).count()
    active_deployments = db.query(Deployment).filter(
        Deployment.status.in_([DeploymentStatus.QUEUED, DeploymentStatus.IN_PROGRESS])
    ).count()
    failed_deployments = db.query(Deployment).filter(Deployment.status == DeploymentStatus.FAILED).count()
    rollbacks = db.query(Deployment).filter(Deployment.status == DeploymentStatus.ROLLED_BACK).count()
    pending_change_requests = db.query(ChangeRequest).filter(
        ChangeRequest.status.in_([ChangeStatus.PENDING_APPROVAL])
    ).count()

    # Alert counts for dashboard stat cards
    active_alerts = db.query(Alert).filter(Alert.resolved == False)
    critical_alerts = active_alerts.filter(Alert.severity == AlertSeverity.CRITICAL).count()
    warning_alerts = active_alerts.filter(Alert.severity == AlertSeverity.WARNING).count()

    # Open drift + circuit-breaker-flagged devices -- both tracked in the
    # DB already (config_drifts.status, devices.flagged_unstable) but
    # previously invisible anywhere on the dashboard.
    open_drifts = db.query(ConfigDrift).filter(ConfigDrift.status == DriftStatus.OPEN).count()
    flagged_unstable_devices_query = (
        db.query(Device).filter(Device.flagged_unstable == True).order_by(Device.unstable_since.desc())
    )
    flagged_unstable_count = flagged_unstable_devices_query.count()
    flagged_unstable_devices = [
        {
            "id": str(d.id),
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "unstable_since": d.unstable_since.isoformat() if d.unstable_since else None,
        }
        for d in flagged_unstable_devices_query.limit(5).all()
    ]

    # --- New Dashboard Widget Data ---

    # 1. Global Health Score & Top CPU/Memory
    all_devices = {d.id: d for d in db.query(Device).all()}
    latest_metrics = vm_client.fleet_latest_metrics()

    metrics_with_device = []
    for dev_id, row in latest_metrics.items():
        if dev_id in all_devices:
            metrics_with_device.append((row, all_devices[dev_id]))

    top_cpu = sorted(metrics_with_device, key=lambda x: x[0].get("cpu_utilization_pct") or 0, reverse=True)[:5]
    top_memory = sorted(metrics_with_device, key=lambda x: x[0].get("memory_utilization_pct") or 0, reverse=True)[:5]
    top_bandwidth = sorted(metrics_with_device, key=lambda x: x[0].get("interface_utilization_pct") or 0, reverse=True)[:5]

    health_scores = [x[0].get("health_score") for x in metrics_with_device if x[0].get("health_score") is not None]
    global_health_score = int(sum(health_scores) / len(health_scores)) if health_scores else 100

    top_cpu_devices = [
        {
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "cpu": row.get("cpu_utilization_pct") or 0,
            "cpu_history": _metric_sparkline(db, d.id, "cpu_utilization_pct"),
        }
        for row, d in top_cpu
    ]
    top_memory_devices = [
        {
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "memory": row.get("memory_utilization_pct") or 0,
            "memory_history": _metric_sparkline(db, d.id, "memory_utilization_pct"),
        }
        for row, d in top_memory
    ]
    top_bandwidth_devices = [
        {
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "bandwidth": row.get("interface_utilization_pct") or 0,
            "bandwidth_history": _metric_sparkline(db, d.id, "interface_utilization_pct"),
        }
        for row, d in top_bandwidth
    ]

    UPLINK_ROLE_PATTERNS = ["wan", "uplink", "edge", "core", "isp", "internet"]
    uplink_tuples = [
        (r, d) for r, d in metrics_with_device
        if d.is_uplink or (d.device_role and any(pat in d.device_role.lower() for pat in UPLINK_ROLE_PATTERNS))
    ]
    uplink_tuples = sorted(uplink_tuples, key=lambda x: x[0].get("interface_utilization_pct") or 0, reverse=True)[:10]

    uplinks = []
    for row, d in uplink_tuples:
        util_pct = row.get("interface_utilization_pct") or 0
        speed_bps = row.get("interface_speed_bps") or 0
        throughput_bps = (util_pct / 100.0) * speed_bps if speed_bps else None
        uplinks.append({
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "role": d.device_role,
            "status": d.status.value if hasattr(d.status, "value") else d.status,
            "utilization_pct": round(util_pct, 1),
            "throughput_bps": throughput_bps,
            "link_speed_bps": speed_bps or None,
            "errors": row.get("interface_errors"),
            "history": _metric_sparkline(db, d.id, "interface_utilization_pct"),
        })

    history_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    now = datetime.datetime.now(datetime.timezone.utc)
    cpu_hist = vm_client.fleet_metric_history_hourly("cpu_utilization_pct", history_since, now)
    mem_hist = vm_client.fleet_metric_history_hourly("memory_utilization_pct", history_since, now)
    bw_hist = vm_client.fleet_metric_history_hourly("interface_utilization_pct", history_since, now)

    hist_by_ts = {}
    for entry in cpu_hist:
        hist_by_ts.setdefault(entry["timestamp"].isoformat(), {})["avg_cpu"] = entry["value"]
    for entry in mem_hist:
        hist_by_ts.setdefault(entry["timestamp"].isoformat(), {})["avg_memory"] = entry["value"]
    for entry in bw_hist:
        hist_by_ts.setdefault(entry["timestamp"].isoformat(), {})["avg_bandwidth"] = entry["value"]

    fleet_health_history = [
        {
            "timestamp": ts,
            "avg_cpu": round(data.get("avg_cpu"), 1) if data.get("avg_cpu") is not None else None,
            "avg_memory": round(data.get("avg_memory"), 1) if data.get("avg_memory") is not None else None,
            "avg_bandwidth": round(data.get("avg_bandwidth"), 1) if data.get("avg_bandwidth") is not None else None,
        }
        for ts, data in sorted(hist_by_ts.items())
    ]
#

    # 2. Deployment Success Rate
    deployments_successful = db.query(Deployment).filter(Deployment.status == DeploymentStatus.SUCCEEDED).count()
    deployments_total_finished = db.query(Deployment).filter(Deployment.status.in_([
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK
    ])).count()

    success_rate = round((deployments_successful / deployments_total_finished * 100), 1) if deployments_total_finished > 0 else 100.0

    # 3. Recent Backups (Snapshots)
    recent_backups_query = db.query(ConfigSnapshot, Device.hostname)\
        .join(Device, Device.id == ConfigSnapshot.device_id)\
        .order_by(desc(ConfigSnapshot.created_at))\
        .limit(5).all()

    recent_backups = [{
        "id": str(r[0].id),
        "version": r[0].version,
        "created_at": r[0].created_at.isoformat() if r[0].created_at else "",
        "hostname": r[1]
    } for r in recent_backups_query]

    # 4. Recent Protocol Operations
    recent_ops_query = db.query(ProtocolOperation, Device.hostname)\
        .outerjoin(Device, Device.id == ProtocolOperation.device_id)\
        .order_by(desc(ProtocolOperation.created_at))\
        .limit(5).all()

    recent_protocol_operations = [{
        "id": str(r[0].id),
        "protocol": r[0].protocol.value if hasattr(r[0].protocol, "value") else r[0].protocol,
        "operation": r[0].operation,
        "success": r[0].success,
        "created_at": r[0].created_at.isoformat() if r[0].created_at else "",
        "operator": r[0].operator,
        "device_hostname": r[1] or "Unknown"
    } for r in recent_ops_query]

    # 5. EOL/EOS fleet rollup -- "how many devices are running software
    # past its vendor-published support date", the dashboard-badge
    # version of GET /devices/eol-summary (which has the full per-device
    # breakdown; this just needs the count for the badge, so it doesn't
    # duplicate the per-device detail work here).
    from app.services import eol_service

    eos_count = 0
    for d in db.query(Device.vendor, Device.model, Device.os_version).all():
        status = eol_service.check_device_eol(
            vendor=d.vendor.value if d.vendor else None, model=d.model, os_version=d.os_version,
        )
        if status.is_eos:
            eos_count += 1

    # --- NOC Live Status: down ports from interface_statuses table ---
    # Get the latest status row per (device_id, if_index) and filter to
    # status == down. This gives a real-time "ports currently down" list
    # backed by SNMP poll data rather than relying solely on alert rows.
    latest_if_subq = (
        db.query(
            InterfaceStatus.device_id,
            InterfaceStatus.if_index,
            func.max(InterfaceStatus.changed_at).label("latest_at"),
        )
        .group_by(InterfaceStatus.device_id, InterfaceStatus.if_index)
        .subquery()
    )
    down_port_rows = (
        db.query(InterfaceStatus.if_descr, InterfaceStatus.changed_at, Device.hostname)
        .join(
            latest_if_subq,
            (InterfaceStatus.device_id == latest_if_subq.c.device_id)
            & (InterfaceStatus.if_index == latest_if_subq.c.if_index)
            & (InterfaceStatus.changed_at == latest_if_subq.c.latest_at),
        )
        .join(Device, Device.id == InterfaceStatus.device_id)
        .filter(InterfaceStatus.status == 'down')
        .order_by(InterfaceStatus.changed_at.desc())
        .limit(20)
        .all()
    )
    down_ports = [
        {
            "hostname": r.hostname,
            "interface": r.if_descr,
            "down_since": r.changed_at.isoformat() if r.changed_at else None,
        }
        for r in down_port_rows
    ]

    # Recent device reboots: devices whose latest uptime reading is under
    # 1 hour (3600s), indicating a recent restart.
    reboot_threshold = 3600
    recent_reboots_tuples = [
        (row, d) for row, d in metrics_with_device
        if row.get("uptime_seconds") is not None and row["uptime_seconds"] < reboot_threshold
    ]
    recent_reboots_tuples = sorted(recent_reboots_tuples, key=lambda x: x[0]["uptime_seconds"])[:10]
    recent_reboots = [
        {
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "uptime_seconds": row["uptime_seconds"],
            "polled_at": row.get("polled_at").isoformat() if row.get("polled_at") else None,
        }
        for row, d in recent_reboots_tuples
    ]

    # --- Instant-troubleshooting additions ---

    # 6. Offline/degraded devices, by name -- devices_online/devices_total
    # above only give a count; when something's actually down, the first
    # thing anyone needs is *which* device and *since when*, without
    # having to jump to the full Inventory page and filter. Ordered by
    # least-recently-seen first (longest outages surface at the top).
    offline_rows = (
        db.query(Device)
        .filter(Device.status.in_([DeviceStatus.OFFLINE, DeviceStatus.DEGRADED]))
        .order_by(Device.last_reachability_poll_at.asc().nulls_first())
        .limit(15)
        .all()
    )
    offline_devices = [
        {
            "id": str(d.id),
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "status": d.status.value if hasattr(d.status, "value") else d.status,
            "last_seen": d.last_reachability_poll_at.isoformat() if d.last_reachability_poll_at else None,
            "last_error": d.last_snmp_poll_error,
        }
        for d in offline_rows
    ]

    # 7. Top interface-error devices -- interface_errors is already
    # collected every poll (used today only to sort the Top Bandwidth
    # widget's tie-breaks) but was never surfaced on its own. CRC/input
    # errors climbing on a link is a classic "why is this connection
    # flaky" signal independent of raw utilization, so it deserves its
    # own instant-triage list rather than being buried inside bandwidth.
    top_errors_tuples = sorted(
        [x for x in metrics_with_device if (x[0].get("interface_errors") or 0) > 0],
        key=lambda x: x[0].get("interface_errors") or 0,
        reverse=True
    )[:5]
    top_error_devices = [
        {
            "hostname": d.hostname,
            "ip_address": d.ip_address,
            "interface_errors": row.get("interface_errors") or 0,
        }
        for row, d in top_errors_tuples
    ]

    # 8. Flapping interfaces (last 24h) -- interface_statuses is a
    # transition log (one row per up/down change), so counting rows per
    # (device_id, if_index) in the window directly answers "which port is
    # bouncing" -- the single most common cause of intermittent
    # connectivity complaints, and previously only visible one device at a
    # time on that device's own history tab.
    flap_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    flap_rows = (
        db.query(
            InterfaceStatus.device_id,
            InterfaceStatus.if_descr,
            Device.hostname,
            func.count(InterfaceStatus.id).label("flap_count"),
            func.max(InterfaceStatus.changed_at).label("last_change"),
        )
        .join(Device, Device.id == InterfaceStatus.device_id)
        .filter(InterfaceStatus.changed_at >= flap_since, InterfaceStatus.is_transition == True)
        .group_by(InterfaceStatus.device_id, InterfaceStatus.if_descr, Device.hostname)
        .having(func.count(InterfaceStatus.id) > 1)
        .order_by(desc("flap_count"))
        .limit(10)
        .all()
    )
    flapping_interfaces = [
        {
            "hostname": r.hostname,
            "interface": r.if_descr,
            "flap_count": r.flap_count,
            "last_change": r.last_change.isoformat() if r.last_change else None,
        }
        for r in flap_rows
    ]

    # 9. Severity-weighted fleet health -- devices_online/devices_total
    # (and the top "Online" stat card) only answer "is it reachable right
    # now", so a device that's technically online but has a port flapping
    # every few minutes, a down interface, or is already flagged by the
    # instability circuit-breaker counts as a full "healthy" device -- the
    # 100% tile can lie. This blends those already-tracked signals into
    # one score: a clean online device is worth 1.0, an online-but-flaky
    # one (flagged unstable, has a currently-down port, or an interface
    # that's flapped >1x in the last 24h) is worth 0.5, DEGRADED is worth
    # 0.5, UNKNOWN is worth 0.25 (no recent poll to vouch for it), and
    # OFFLINE is worth 0.
    flapping_device_ids = {r.device_id for r in flap_rows}
    down_port_device_ids = {
        r.device_id
        for r in db.query(InterfaceStatus.device_id)
        .join(
            latest_if_subq,
            (InterfaceStatus.device_id == latest_if_subq.c.device_id)
            & (InterfaceStatus.if_index == latest_if_subq.c.if_index)
            & (InterfaceStatus.changed_at == latest_if_subq.c.latest_at),
        )
        .filter(InterfaceStatus.status == "down")
        .all()
    }
    flagged_unstable_ids = {d.id for d in db.query(Device.id).filter(Device.flagged_unstable == True).all()}

    healthy_count = degraded_count = offline_count = unknown_count = 0
    weighted_sum = 0.0
    for d in all_devices.values():
        if d.status == DeviceStatus.OFFLINE:
            offline_count += 1
        elif d.status == DeviceStatus.UNKNOWN:
            unknown_count += 1
            weighted_sum += 0.25
        elif d.status == DeviceStatus.DEGRADED:
            degraded_count += 1
            weighted_sum += 0.5
        elif d.id in flagged_unstable_ids or d.id in flapping_device_ids or d.id in down_port_device_ids:
            # ONLINE by reachability, but flaky by one of the signals above.
            degraded_count += 1
            weighted_sum += 0.5
        else:
            healthy_count += 1
            weighted_sum += 1.0
    fleet_health_weighted_pct = round((weighted_sum / devices_total) * 100, 1) if devices_total else 100.0

    return {
        "devices_online": devices_online,
        "offline_devices": offline_devices,
        "top_error_devices": top_error_devices,
        "flapping_interfaces": flapping_interfaces,
        "devices_total": devices_total,
        "active_deployments": active_deployments,
        "failed_deployments": failed_deployments,
        "rollbacks": rollbacks,
        "pending_change_requests": pending_change_requests,
        "critical_alerts": critical_alerts,
        "warning_alerts": warning_alerts,
        "open_drifts": open_drifts,
        "flagged_unstable_count": flagged_unstable_count,
        "flagged_unstable_devices": flagged_unstable_devices,
        "eos_device_count": eos_count,

        "global_health_score": global_health_score,
        "deployment_success_rate": success_rate,
        "top_cpu_devices": top_cpu_devices,
        "top_memory_devices": top_memory_devices,
        "top_bandwidth_devices": top_bandwidth_devices,
        "uplinks": uplinks,
        "uplink_availability": _uplink_availability(db),
        "ipam_overview": _ipam_overview_cached(db),
        "down_ports": down_ports,
        "recent_reboots": recent_reboots,
        "fleet_health_history": fleet_health_history,
        "recent_backups": recent_backups,
        "recent_protocol_operations": recent_protocol_operations,

        "fleet_health_weighted_pct": fleet_health_weighted_pct,
        "fleet_health_breakdown": {
            "healthy": healthy_count,
            "degraded": degraded_count,
            "offline": offline_count,
            "unknown": unknown_count,
        },
    }


@router.get("/preferences", response_model=DashboardPreferenceRead)
def get_dashboard_preferences(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns this user's dashboard widget layout (which widgets show,
    in what order) plus the full widget catalog so the frontend's
    customize panel can render human-readable labels without hardcoding
    the registry a second time. A user who has never customized
    anything gets the registry's default layout back -- no row is
    created until they actually save one via PUT.
    """
    # available_widgets always comes from the in-memory registry (no DB
    # dependency), so it's built up front and returned even if the
    # user's saved-preference row can't be read below -- e.g. because a
    # migration adding a column here (like `thresholds`) hasn't been
    # applied to this deployment's DB yet. Previously a DB error here
    # took down the whole response, the frontend silently swallowed it,
    # and the dashboard rendered with no widgets and nothing to
    # customize -- with no indication anywhere of why.
    available_widgets = [
        DashboardWidgetInfo(id=w.id, title=w.title, data_source=w.data_source, default_visible=w.default_visible)
        for w in dashboard_widgets.DASHBOARD_WIDGETS
    ]

    try:
        pref = db.query(DashboardPreference).filter(DashboardPreference.user_id == current_user.id).first()
        if pref is None:
            layout = dashboard_widgets.default_layout()
            thresholds = dashboard_widgets.default_thresholds()
        else:
            try:
                saved = json.loads(pref.layout)
            except (ValueError, TypeError):
                saved = []
            layout = dashboard_widgets.merge_layout(saved)
            try:
                saved_thresholds = json.loads(pref.thresholds or "{}")
            except (ValueError, TypeError):
                saved_thresholds = {}
            thresholds = dashboard_widgets.merge_thresholds(saved_thresholds)
    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            "get_dashboard_preferences: failed to read saved preferences for user %s -- "
            "falling back to registry defaults (likely a pending migration on this DB)",
            current_user.id,
        )
        layout = dashboard_widgets.default_layout()
        thresholds = dashboard_widgets.default_thresholds()

    return DashboardPreferenceRead(
        layout=[DashboardLayoutEntry(**e) for e in layout],
        available_widgets=available_widgets,
        thresholds=thresholds,
    )

@router.put("/preferences", response_model=DashboardPreferenceRead)
def set_dashboard_preferences(
    payload: DashboardPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Saves this user's widget selection/order. Reconciled through
    dashboard_widgets.merge_layout before storing, so a stale client
    payload referencing a since-removed widget id doesn't get persisted,
    and any widget the client's payload omitted is appended rather than
    silently dropped from the user's dashboard.
    """
    merged = dashboard_widgets.merge_layout([e.model_dump() for e in payload.layout])

    pref = db.query(DashboardPreference).filter(DashboardPreference.user_id == current_user.id).first()

    # Thresholds: omitted entirely -> keep whatever's already saved (or the
    # registry default for a brand-new pref row); provided -> merge/validate
    # against the registry so a bad payload can't leave a gauge stuck on
    # one color.
    if payload.thresholds is not None:
        merged_thresholds = dashboard_widgets.merge_thresholds(payload.thresholds.model_dump())
    elif pref is not None:
        try:
            merged_thresholds = dashboard_widgets.merge_thresholds(json.loads(pref.thresholds or "{}"))
        except (ValueError, TypeError):
            merged_thresholds = dashboard_widgets.default_thresholds()
    else:
        merged_thresholds = dashboard_widgets.default_thresholds()

    if pref is None:
        pref = DashboardPreference(
            user_id=current_user.id, layout=json.dumps(merged), thresholds=json.dumps(merged_thresholds)
        )
        db.add(pref)
    else:
        pref.layout = json.dumps(merged)
        pref.thresholds = json.dumps(merged_thresholds)
    db.commit()

    return DashboardPreferenceRead(
        layout=[DashboardLayoutEntry(**e) for e in merged],
        available_widgets=[
            DashboardWidgetInfo(id=w.id, title=w.title, data_source=w.data_source, default_visible=w.default_visible)
            for w in dashboard_widgets.DASHBOARD_WIDGETS
        ],
        thresholds=merged_thresholds,
    )


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return _compute_summary(db)


# --- "What changed since I was last here" timeline -----------------------
#
# Merges alerts, change requests, config drift, and deployments -- the four
# things that make up "did anything happen overnight" -- into one
# time-sorted feed. Previously answering that question meant checking the
# Alert Center, Change Requests, Drift, and Deployments pages separately;
# each of those still has its own full page for filtering/detail, this is
# just the merged skim view for the dashboard.
TIMELINE_DEFAULT_LIMIT = 40
TIMELINE_MAX_LIMIT = 200
# Fetch this many of the most-recent rows per source before merging, so the
# merge/sort itself stays cheap even if `limit` is small -- without this an
# `since` filter far in the past on a source with very few new rows would
# still be fine, but a plain "give me the last 10" from an active fleet
# would otherwise have to sort every alert ever raised.
_PER_SOURCE_FETCH_MULTIPLIER = 3


def _compute_timeline(db: Session, limit: int, since: "datetime.datetime | None") -> list[dict]:
    fetch_n = min(limit * _PER_SOURCE_FETCH_MULTIPLIER, TIMELINE_MAX_LIMIT * _PER_SOURCE_FETCH_MULTIPLIER)
    events: list[dict] = []

    alert_q = db.query(Alert, Device.hostname).outerjoin(Device, Device.id == Alert.device_id)
    if since is not None:
        alert_q = alert_q.filter(Alert.created_at >= since)
    for a, hostname in alert_q.order_by(desc(Alert.created_at)).limit(fetch_n).all():
        events.append({
            "type": "alert",
            "severity": a.severity.value if hasattr(a.severity, "value") else a.severity,
            "timestamp": a.created_at.isoformat() if a.created_at else None,
            "title": a.category,
            "detail": a.message,
            "hostname": hostname,
            "link": "/alerts",
        })

    cr_q = db.query(ChangeRequest, Device.hostname).outerjoin(Device, Device.id == ChangeRequest.device_id)
    if since is not None:
        cr_q = cr_q.filter(ChangeRequest.created_at >= since)
    for cr, hostname in cr_q.order_by(desc(ChangeRequest.created_at)).limit(fetch_n).all():
        status = cr.status.value if hasattr(cr.status, "value") else cr.status
        events.append({
            "type": "change_request",
            "severity": {"failed": "critical", "rolled_back": "warning", "pending_approval": "warning"}.get(status, "info"),
            "timestamp": cr.created_at.isoformat() if cr.created_at else None,
            "title": f"Change request {status.replace('_', ' ')}",
            "detail": cr.description,
            "hostname": hostname,
            "link": f"/change-requests/{cr.id}",
        })

    drift_q = db.query(ConfigDrift, Device.hostname).outerjoin(Device, Device.id == ConfigDrift.device_id)
    if since is not None:
        drift_q = drift_q.filter(ConfigDrift.detected_at >= since)
    for d, hostname in drift_q.order_by(desc(ConfigDrift.detected_at)).limit(fetch_n).all():
        severity = d.severity.value if hasattr(d.severity, "value") else d.severity
        events.append({
            "type": "drift",
            "severity": "critical" if severity in ("high", "critical") else "warning" if severity == "medium" else "info",
            "timestamp": d.detected_at.isoformat() if d.detected_at else None,
            "title": f"Config drift detected ({severity})",
            "detail": d.ai_summary or f"+{d.added_lines}/-{d.removed_lines} lines vs baseline",
            "hostname": hostname,
            "link": "/drift",
        })

    dep_q = db.query(Deployment, Device.hostname).outerjoin(Device, Device.id == Deployment.device_id)
    if since is not None:
        dep_q = dep_q.filter(Deployment.created_at >= since)
    for dep, hostname in dep_q.order_by(desc(Deployment.created_at)).limit(fetch_n).all():
        status = dep.status.value if hasattr(dep.status, "value") else dep.status
        events.append({
            "type": "deployment",
            "severity": "critical" if status == "failed" else "warning" if status == "rolled_back" else "info",
            "timestamp": dep.created_at.isoformat() if dep.created_at else None,
            "title": f"Deployment {status.replace('_', ' ')}",
            "detail": dep.error_message,
            "hostname": hostname,
            "link": "/deployments",
        })

    # Syslog message-rate and traffic bandwidth anomalies -- both pages are
    # otherwise "invisible" unless someone happens to load them while the
    # spike is happening (see the module docstrings on
    # syslog_service.detect_message_rate_anomalies and
    # flow_service.detect_bandwidth_anomalies). Detection always runs
    # against a fixed recent/baseline window rather than `since`, since an
    # anomaly is inherently "right now", not something to page back
    # through -- `since` only gates whether it's included in this
    # response, same as every other event type here.
    try:
        for a in syslog_service.detect_message_rate_anomalies(db):
            ts = a["detected_at"]
            if since is not None and ts < since.isoformat():
                continue
            events.append({
                "type": "anomaly",
                "severity": "warning",
                "timestamp": ts,
                "title": "Syslog anomaly detected",
                "detail": f"{a['hostname'] or 'Unknown device'} logging {a['multiplier']}x its normal rate ({a['recent_count']} msgs)",
                "hostname": a["hostname"],
                "link": "/syslog",
            })
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("syslog anomaly detection failed for dashboard timeline")

    try:
        for a in flow_service.detect_bandwidth_anomalies(db):
            ts = a["detected_at"]
            if since is not None and ts < since.isoformat():
                continue
            events.append({
                "type": "anomaly",
                "severity": "warning",
                "timestamp": ts,
                "title": "Traffic anomaly detected",
                "detail": f"{a['ip_address']} pushing {a['multiplier']}x its normal bandwidth",
                "hostname": None,
                "link": "/traffic",
            })
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("flow anomaly detection failed for dashboard timeline")

    events = [e for e in events if e["timestamp"]]
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


@router.get("/timeline")
def get_timeline(
    limit: int = Query(TIMELINE_DEFAULT_LIMIT, ge=1, le=TIMELINE_MAX_LIMIT),
    since_hours: int | None = Query(None, ge=1, le=24 * 30, description="Only include events from the last N hours"),
    db: Session = Depends(get_db),
):
    since = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)
        if since_hours is not None
        else None
    )
    return _compute_timeline(db, limit=limit, since=since)


async def _heartbeat_loop(websocket: WebSocket):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            await websocket.send_json(_compute_summary(db))
        finally:
            db.close()


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket, token: str = Query("")):
    # Was accepting every connection unauthenticated -- this pushes the
    # live fleet summary (device counts, health, alert volume) to anyone
    # who could reach the API at all, no login required. Same fix as
    # app.api.terminal.device_terminal: resolve+check before accept().
    db = SessionLocal()
    try:
        user = get_current_user_ws(token, db)
    finally:
        db.close()
    if not user:
        await websocket.close(code=1008)  # Policy Violation
        return

    await websocket.accept()

    db = SessionLocal()
    try:
        await websocket.send_json(_compute_summary(db))
    finally:
        db.close()

    redis_client = event_bus.get_async_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(event_bus.DASHBOARD_CHANNEL)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None:
                continue
            db = SessionLocal()
            try:
                await websocket.send_json(_compute_summary(db))
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pubsub.unsubscribe(event_bus.DASHBOARD_CHANNEL)
        await pubsub.close()
        await redis_client.close()
