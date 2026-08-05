"""Topology-aware alert correlation.

Problem: when a core device drops off the network entirely, every device
that's only reachable *through* it starts failing its own health polls a
cycle or two later and raises its own independent alert. Without
correlation, one physical failure shows up in Alert Center as a dozen
equally-urgent, seemingly-unrelated alerts -- exactly the "storm" gap
called out against Auvik/SolarWinds, which both use topology to collapse
this down to one root cause with the rest flagged as impacted.

Approach, using the same topology graph the Topology page already builds
(app.services.topology_service.build_topology, from confirmed LLDP/CDP/
GNS3/subnet-inferred adjacency):

  1. A new CRITICAL alert lands for some device D in one of
     ROOT_CAUSE_CATEGORIES (device-down-shaped conditions).
  2. Remove D from the topology graph and find the connected components of
     what's left.
  3. The component containing the most currently-"up" devices is treated
     as "core" -- it's still independently connected to a healthy part of
     the network, so D failing doesn't fully explain its state.
  4. Every *other* component was, by construction, only linked to the
     core through D. Any active (unresolved) alert on a device in one of
     those components is marked suppressed, pointing at D's alert as its
     root cause.

This is a heuristic, not a certified single-fault root-cause engine --
multi-homed devices in an "impacted" component that happen to have another
path out won't be un-suppressed until D recovers. It optimizes for the
common case (single point of failure fans out to a stub of dependents)
which is what actually floods Alert Center in practice.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSeverity
from app.models.device import Device
from app.services import event_bus, topology_service

# Categories that mean "this device itself is down / unreachable", as
# opposed to a threshold breach on a device that's still up and talking --
# only the former can plausibly explain *other* devices going dark too.
ROOT_CAUSE_CATEGORIES = {"Device Unreachable", "Power Failure"}


def _build_adjacency(db: Session) -> dict[str, set[str]]:
    graph = topology_service.build_topology(db)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)
    return adjacency


def _connected_components(nodes: set[str], adjacency: dict[str, set[str]]) -> list[set[str]]:
    seen: set[str] = set()
    components: list[set[str]] = []
    for start in nodes:
        if start in seen:
            continue
        component: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            for neighbor in adjacency.get(node, ()):
                if neighbor in nodes and neighbor not in component:
                    stack.append(neighbor)
        seen |= component
        components.append(component)
    return components


def correlate_downstream(db: Session, root_alert: Alert) -> list[uuid.UUID]:
    """Given a freshly-raised/escalated critical device-down alert, finds
    devices that are topologically stranded by that failure and marks
    their active alerts as suppressed under it.

    Returns the list of alert ids that were suppressed (for logging/tests;
    callers don't need to do anything with it).
    """
    if root_alert.device_id is None:
        return []
    if root_alert.severity != AlertSeverity.CRITICAL:
        return []
    if root_alert.category not in ROOT_CAUSE_CATEGORIES:
        return []

    root_id = str(root_alert.device_id)
    adjacency = _build_adjacency(db)
    if root_id not in adjacency or not adjacency[root_id]:
        return []  # isolated node in the graph -- nothing depends on it

    other_nodes = set(adjacency.keys()) - {root_id}
    # Also drop root's own edges when building the post-failure graph.
    trimmed_adjacency = {n: (neighbors - {root_id}) for n, neighbors in adjacency.items() if n != root_id}
    components = _connected_components(other_nodes, trimmed_adjacency)
    if len(components) <= 1:
        return []  # root wasn't a cut point -- everything else still connects

    # Rank components by how many currently-"up" devices they contain;
    # the healthiest component is assumed to still have an independent
    # path to the rest of the network and is treated as "core".
    statuses = {
        str(d.id): (d.status.value if d.status else "")
        for d in db.query(Device.id, Device.status).filter(Device.id.in_([uuid.UUID(n) for n in other_nodes])).all()
    }
    components.sort(key=lambda comp: sum(1 for n in comp if statuses.get(n) == "online"), reverse=True)
    impacted_device_ids = {uuid.UUID(n) for comp in components[1:] for n in comp}
    if not impacted_device_ids:
        return []

    victims = (
        db.query(Alert)
        .filter(
            Alert.device_id.in_(impacted_device_ids),
            Alert.resolved == False,  # noqa: E712
            Alert.id != root_alert.id,
        )
        .all()
    )
    suppressed_ids: list[uuid.UUID] = []
    for alert in victims:
        if alert.suppressed and alert.root_cause_alert_id == root_alert.id:
            continue
        alert.suppressed = True
        alert.root_cause_alert_id = root_alert.id
        suppressed_ids.append(alert.id)

    if suppressed_ids:
        db.commit()
        event_bus.publish_event(
            "alerts_correlated",
            root_alert_id=str(root_alert.id),
            suppressed_count=len(suppressed_ids),
            channel=event_bus.ALERTS_CHANNEL,
        )
    return suppressed_ids


def release_suppressed(db: Session, root_alert: Alert) -> int:
    """When a root-cause alert resolves (device came back), un-suppress
    everything that was pointing at it -- they're independent conditions
    again and should reappear as normal alerts if still active."""
    dependents = db.query(Alert).filter(Alert.root_cause_alert_id == root_alert.id).all()
    if not dependents:
        return 0
    for alert in dependents:
        alert.suppressed = False
        alert.root_cause_alert_id = None
    db.commit()
    event_bus.publish_event(
        "alerts_correlated",
        root_alert_id=str(root_alert.id),
        suppressed_count=0,
        channel=event_bus.ALERTS_CHANNEL,
    )
    return len(dependents)