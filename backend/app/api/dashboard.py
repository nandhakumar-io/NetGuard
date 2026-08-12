import asyncio
import contextlib
import datetime
import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user, get_current_user_ws
from app.models.alert import Alert, AlertSeverity
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.dashboard_preference import DashboardPreference
from app.models.deployment import Deployment, DeploymentStatus
from app.models.device import Device, DeviceStatus
from app.models.device_metric import DeviceMetric
from app.models.interface_status import InterfaceStatus
from app.models.protocol_operation import ProtocolOperation
from app.models.snapshot import ConfigSnapshot
from app.models.user import User
from app.schemas.dashboard_preference import (
    DashboardLayoutEntry,
    DashboardPreferenceRead,
    DashboardPreferenceUpdate,
    DashboardWidgetInfo,
)
from app.services import dashboard_widgets, event_bus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

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
    column = getattr(DeviceMetric, column_name)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=SPARKLINE_LOOKBACK_HOURS)
    rows = (
        db.query(column, DeviceMetric.polled_at)
        .filter(DeviceMetric.device_id == device_id, DeviceMetric.polled_at >= since)
        .order_by(DeviceMetric.polled_at.desc())
        .limit(SPARKLINE_MAX_POINTS)
        .all()
    )
    values = [r[0] for r in reversed(rows) if r[0] is not None]
    return values


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
    # Get the single latest metric row for each device using a subquery
    latest_metrics_subq = db.query(
        DeviceMetric.device_id,
        func.max(DeviceMetric.polled_at).label("latest_polled_at")
    ).group_by(DeviceMetric.device_id).subquery()

    latest_metrics_query = db.query(DeviceMetric, Device.hostname, Device.ip_address)\
        .join(latest_metrics_subq,
             (DeviceMetric.device_id == latest_metrics_subq.c.device_id) &
             (DeviceMetric.polled_at == latest_metrics_subq.c.latest_polled_at))\
        .join(Device, Device.id == DeviceMetric.device_id)\
        .all()

    top_cpu = sorted(latest_metrics_query, key=lambda x: (x[0].cpu_utilization_pct or 0), reverse=True)[:5]
    top_memory = sorted(latest_metrics_query, key=lambda x: (x[0].memory_utilization_pct or 0), reverse=True)[:5]
    top_bandwidth = sorted(latest_metrics_query, key=lambda x: (x[0].interface_utilization_pct or 0), reverse=True)[:5]

    health_scores = [x[0].health_score for x in latest_metrics_query if x[0].health_score is not None]
    global_health_score = int(sum(health_scores) / len(health_scores)) if health_scores else 100

    # Sparkline history: last-hour CPU/memory trend for just the Top-N
    # devices (not the whole fleet), so the widget shows shape-of-trend
    # alongside the current value instead of only a static snapshot.
    # Scoped to the handful of top device_ids rather than fetching full
    # metric_history per device, since this runs on every dashboard
    # summary poll/websocket push.
    top_cpu_devices = [
        {
            "hostname": r[1],
            "ip_address": r[2],
            "cpu": r[0].cpu_utilization_pct or 0,
            "cpu_history": _metric_sparkline(db, r[0].device_id, "cpu_utilization_pct"),
        }
        for r in top_cpu
    ]
    top_memory_devices = [
        {
            "hostname": r[1],
            "ip_address": r[2],
            "memory": r[0].memory_utilization_pct or 0,
            "memory_history": _metric_sparkline(db, r[0].device_id, "memory_utilization_pct"),
        }
        for r in top_memory
    ]
    top_bandwidth_devices = [
        {
            "hostname": r[1],
            "ip_address": r[2],
            "bandwidth": r[0].interface_utilization_pct or 0,
            "bandwidth_history": _metric_sparkline(db, r[0].device_id, "interface_utilization_pct"),
        }
        for r in top_bandwidth
    ]

    # 1b. Uplinks / WAN links -- devices whose device_role marks them as
    # the fleet's edge/uplink/WAN-facing boxes (core, distribution,
    # wan-edge, uplink, etc.) get their own rollup distinct from the
    # generic Top Bandwidth widget above: this is "is my WAN link
    # saturated/down", not "which device happens to be busiest right
    # now". Throughput is derived the same way SNMP Health Dashboard
    # detail views do -- interface_utilization_pct against the reported
    # interface_speed_bps -- so it lines up with what /devices shows for
    # the same device.
    UPLINK_ROLE_PATTERNS = ["wan", "uplink", "edge", "core", "isp", "internet"]
    uplink_role_filter = func.lower(Device.device_role).contains(UPLINK_ROLE_PATTERNS[0])
    for pattern in UPLINK_ROLE_PATTERNS[1:]:
        uplink_role_filter = uplink_role_filter | func.lower(Device.device_role).contains(pattern)

    uplink_rows = (
        db.query(DeviceMetric, Device.hostname, Device.ip_address, Device.device_role, Device.status)
        .join(
            latest_metrics_subq,
            (DeviceMetric.device_id == latest_metrics_subq.c.device_id)
            & (DeviceMetric.polled_at == latest_metrics_subq.c.latest_polled_at),
        )
        .join(Device, Device.id == DeviceMetric.device_id)
        .filter(Device.device_role.isnot(None), uplink_role_filter)
        .order_by(desc(DeviceMetric.interface_utilization_pct))
        .limit(10)
        .all()
    )

    uplinks = []
    for metric, hostname, ip_address, device_role, status in uplink_rows:
        util_pct = metric.interface_utilization_pct or 0
        speed_bps = metric.interface_speed_bps or 0
        throughput_bps = (util_pct / 100.0) * speed_bps if speed_bps else None
        uplinks.append({
            "hostname": hostname,
            "ip_address": ip_address,
            "role": device_role,
            "status": status.value if hasattr(status, "value") else status,
            "utilization_pct": round(util_pct, 1),
            "throughput_bps": throughput_bps,
            "link_speed_bps": speed_bps or None,
            "errors": metric.interface_errors,
            "history": _metric_sparkline(db, metric.device_id, "interface_utilization_pct"),
        })

    # 1c. Fleet health history -- hourly-bucketed fleet-wide average
    # CPU/memory/bandwidth utilization over the last 24h, so the
    # dashboard has an actual trend graph rather than only point-in-time
    # Top-N cards. Bucketed in SQL (not per-row in Python) since this
    # scans device_metrics across the whole fleet.
    history_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    bucket = func.date_trunc("hour", DeviceMetric.polled_at)
    history_rows = (
        db.query(
            bucket.label("bucket"),
            func.avg(DeviceMetric.cpu_utilization_pct).label("avg_cpu"),
            func.avg(DeviceMetric.memory_utilization_pct).label("avg_memory"),
            func.avg(DeviceMetric.interface_utilization_pct).label("avg_bandwidth"),
        )
        .filter(DeviceMetric.polled_at >= history_since)
        .group_by(bucket)
        .order_by(bucket)
        .all()
    )
    fleet_health_history = [
        {
            "timestamp": r.bucket.isoformat() if r.bucket else None,
            "avg_cpu": round(r.avg_cpu, 1) if r.avg_cpu is not None else None,
            "avg_memory": round(r.avg_memory, 1) if r.avg_memory is not None else None,
            "avg_bandwidth": round(r.avg_bandwidth, 1) if r.avg_bandwidth is not None else None,
        }
        for r in history_rows
    ]

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
    recent_reboot_rows = (
        db.query(Device.hostname, Device.ip_address, DeviceMetric.uptime_seconds, DeviceMetric.polled_at)
        .join(
            latest_metrics_subq,
            (DeviceMetric.device_id == latest_metrics_subq.c.device_id)
            & (DeviceMetric.polled_at == latest_metrics_subq.c.latest_polled_at),
        )
        .join(Device, Device.id == DeviceMetric.device_id)
        .filter(DeviceMetric.uptime_seconds.isnot(None), DeviceMetric.uptime_seconds < reboot_threshold)
        .order_by(DeviceMetric.uptime_seconds)
        .limit(10)
        .all()
    )
    recent_reboots = [
        {
            "hostname": r.hostname,
            "ip_address": r.ip_address,
            "uptime_seconds": r.uptime_seconds,
            "polled_at": r.polled_at.isoformat() if r.polled_at else None,
        }
        for r in recent_reboot_rows
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
    top_errors = sorted(
        (r for r in latest_metrics_query if (r[0].interface_errors or 0) > 0),
        key=lambda x: (x[0].interface_errors or 0),
        reverse=True,
    )[:5]
    top_error_devices = [
        {
            "hostname": r[1],
            "ip_address": r[2],
            "interface_errors": r[0].interface_errors or 0,
        }
        for r in top_errors
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
        "down_ports": down_ports,
        "recent_reboots": recent_reboots,
        "fleet_health_history": fleet_health_history,
        "recent_backups": recent_backups,
        "recent_protocol_operations": recent_protocol_operations,
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
    pref = db.query(DashboardPreference).filter(DashboardPreference.user_id == current_user.id).first()
    if pref is None:
        layout = dashboard_widgets.default_layout()
    else:
        try:
            saved = json.loads(pref.layout)
        except (ValueError, TypeError):
            saved = []
        layout = dashboard_widgets.merge_layout(saved)

    return DashboardPreferenceRead(
        layout=[DashboardLayoutEntry(**e) for e in layout],
        available_widgets=[
            DashboardWidgetInfo(id=w.id, title=w.title, data_source=w.data_source, default_visible=w.default_visible)
            for w in dashboard_widgets.DASHBOARD_WIDGETS
        ],
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
    if pref is None:
        pref = DashboardPreference(user_id=current_user.id, layout=json.dumps(merged))
        db.add(pref)
    else:
        pref.layout = json.dumps(merged)
    db.commit()

    return DashboardPreferenceRead(
        layout=[DashboardLayoutEntry(**e) for e in merged],
        available_widgets=[
            DashboardWidgetInfo(id=w.id, title=w.title, data_source=w.data_source, default_visible=w.default_visible)
            for w in dashboard_widgets.DASHBOARD_WIDGETS
        ],
    )


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return _compute_summary(db)


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
