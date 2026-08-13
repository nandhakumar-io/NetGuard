import asyncio
import contextlib
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user, get_current_user_ws
from app.models.topology_snapshot import TopologySnapshot
from app.schemas.topology import (
    TopologyDiffResponse,
    TopologyResponse,
    TopologySnapshotRead,
)
from app.services import event_bus, topology_service

router = APIRouter(prefix="/topology", tags=["topology"])

# How often the websocket re-sends the full graph even with no pub/sub
# event -- catches anything an event publisher might have missed (e.g. a
# manual DB edit, a Celery worker restart mid-task) so a NOC wall display
# left open for hours never drifts far from reality even in the worst case.
HEARTBEAT_INTERVAL_SECONDS = 60


def _build_topology_payload(db: Session) -> TopologyResponse:
    """Shared by GET /topology and the /topology/ws push feed, so the two
    can never drift out of sync with each other."""
    graph = topology_service.build_topology(db)
    return TopologyResponse(
        nodes=[
            {
                "id": n.id,
                "hostname": n.hostname,
                "ip_address": n.ip_address,
                "vendor": n.vendor,
                "site": n.site,
                "device_type": n.device_type,
                "status": n.status,
                "flagged_unstable": n.flagged_unstable,
                "has_config_on_file": n.has_config_on_file,
                "health_color": n.health_color,
                "health_score": n.health_score,
                "data_center": n.data_center,
                "rack": n.rack,
                "device_role": n.device_role,
                "interface_error_rate": n.interface_error_rate,
                "active_alert_severity": n.active_alert_severity,
            }
            for n in graph.nodes
        ],
        edges=[
            {
                "source": e.source,
                "target": e.target,
                "subnet": e.subnet,
                "source_ip": e.source_ip,
                "target_ip": e.target_ip,
                "link_source": e.link_source,
                "local_port": e.local_port,
                "neighbor_port": e.neighbor_port,
                "utilization_pct": e.utilization_pct,
                "last_confirmed_at": e.last_confirmed_at,
                "stale": e.stale,
            }
            for e in graph.edges
        ],
    )


@router.get("", response_model=TopologyResponse)
def get_topology(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide network topology: one node per device, edges inferred
    from shared interface subnets found in each device's latest config
    snapshot (see app.services.topology_service for how links are
    derived -- there's no CDP/LLDP discovery in NetGuard, so this is the
    data-grounded substitute).

    Available to any authenticated user (read-only), consistent with the
    other dashboard/summary endpoints in this API.
    """
    return _build_topology_payload(db)


async def _topology_heartbeat_loop(websocket: WebSocket):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            await websocket.send_json({"type": "topology_snapshot", "data": _build_topology_payload(db).model_dump()})
        finally:
            db.close()


@router.websocket("/ws")
async def topology_ws(websocket: WebSocket, token: str = Query("")):
    """Live feed for the Topology page's NOC-wall-display mode: pushes the
    full graph on connect, then again every time a device is added,
    removed, updated (incl. moved to a new data center/rack), or changes
    status -- see app.services.event_bus.TOPOLOGY_CHANNEL publishers --
    plus a HEARTBEAT_INTERVAL_SECONDS fallback re-send as a safety net.

    Sends the whole graph rather than a diff on every push: fleet sizes
    here are small (tens, not thousands of devices, per layoutNodes'
    docstring in Topology.tsx) so this is cheap, and it keeps the client
    trivially simple -- no incremental-patch state machine to get out of
    sync.
    """
    # Was accepting every connection unauthenticated -- handing out live
    # hostname+IP topology for the whole fleet (see _build_topology_payload)
    # to anyone who could reach the API, no login required. See
    # app.api.dashboard.dashboard_ws for the same fix and why.
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
        await websocket.send_json({"type": "topology_snapshot", "data": _build_topology_payload(db).model_dump()})
    finally:
        db.close()

    redis_client = event_bus.get_async_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(event_bus.TOPOLOGY_CHANNEL)

    heartbeat_task = asyncio.create_task(_topology_heartbeat_loop(websocket))

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None:
                continue
            db = SessionLocal()
            try:
                await websocket.send_json({"type": "topology_snapshot", "data": _build_topology_payload(db).model_dump()})
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pubsub.unsubscribe(event_bus.TOPOLOGY_CHANNEL)
        await pubsub.close()
        await redis_client.close()


@router.get("/snapshots", response_model=list[TopologySnapshotRead])
def list_snapshots(
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """History for the Topology page's diff picker -- newest first."""
    rows = db.query(TopologySnapshot).order_by(TopologySnapshot.captured_at.desc()).limit(limit).all()
    return [
        TopologySnapshotRead(
            id=str(s.id),
            node_count=s.node_count,
            edge_count=s.edge_count,
            captured_at=s.captured_at.isoformat() if s.captured_at else None,
        )
        for s in rows
    ]


@router.post("/snapshots", response_model=TopologySnapshotRead)
def create_snapshot(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Captures the current graph on demand -- e.g. right before a
    maintenance window, so "since this snapshot" diffing has a clean
    baseline instead of waiting for the next scheduled capture (see
    settings.TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS in app.main).
    """
    snapshot = topology_service.capture_snapshot(db)
    return TopologySnapshotRead(
        id=str(snapshot.id),
        node_count=snapshot.node_count,
        edge_count=snapshot.edge_count,
        captured_at=snapshot.captured_at.isoformat() if snapshot.captured_at else None,
    )


@router.get("/diff", response_model=TopologyDiffResponse)
def diff_topology(
    older_id: str | None = Query(None, description="Snapshot id to diff from. Defaults to the oldest snapshot within `days`."),
    newer_id: str | None = Query(None, description="Snapshot id to diff to. Defaults to the most recent snapshot."),
    days: int = Query(7, ge=1, le=90, description="When older_id isn't given, pick the oldest snapshot at least this many days back."),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """'What changed in the network graph since <period>' -- the
    historical counterpart to the live /topology view. With no
    parameters, compares the most recent snapshot against the oldest
    one still within the last `days` days (default a week), which is
    what "since last week" means for a caller that doesn't want to look
    up snapshot ids first.
    """
    import datetime

    if newer_id:
        newer = db.get(TopologySnapshot, uuid.UUID(newer_id))
    else:
        newer = db.query(TopologySnapshot).order_by(TopologySnapshot.captured_at.desc()).first()
    if newer is None:
        raise HTTPException(404, "No topology snapshots captured yet")

    if older_id:
        older = db.get(TopologySnapshot, uuid.UUID(older_id))
    else:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        older = (
            db.query(TopologySnapshot)
            .filter(TopologySnapshot.captured_at <= cutoff)
            .order_by(TopologySnapshot.captured_at.desc())
            .first()
        )
        if older is None:
            # Nothing old enough yet (app freshly deployed) -- fall back to
            # the very oldest snapshot on file rather than erroring, so the
            # diff view still shows *something* instead of a dead end.
            older = db.query(TopologySnapshot).order_by(TopologySnapshot.captured_at.asc()).first()
    if older is None or older.id == newer.id:
        raise HTTPException(404, "Not enough snapshot history yet to diff -- check back after the next capture.")

    return topology_service.diff_snapshots(older, newer)
