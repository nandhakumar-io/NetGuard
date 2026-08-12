"""Network Topology view.

Edges are built from three sources, in order of trust:

  1. Confirmed LLDP/CDP neighbor discovery (app.models.discovered_neighbor,
     populated by app.api.devices.discover_device via SNMP) -- real,
     device-reported adjacency.
  2. Imported GNS3 lab link topology (app.services.gns3_service.list_links)
     for any device that was created from/bootstrapped in a GNS3 project
     (Device.gns3_project_id/gns3_node_id set) -- GNS3 knows its own
     virtual cabling exactly, so this is just as authoritative as
     LLDP/CDP for lab devices, and catches links a lab node hasn't (or
     can't) run SNMP Discovery against yet.
  3. Shared-interface-subnet inference (risk_engine.parse_config over each
     device's latest config snapshot) as a last-resort fallback for
     devices with neither of the above -- two devices that each have an
     interface configured into the same subnet are, by construction, on
     the same L2/L3 segment.

Devices with no snapshot on file yet still appear as nodes (inventory-only,
no edges) rather than being dropped, so the graph always reflects the full
fleet.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core import vm_client
from app.models.device import Device
from app.models.discovered_neighbor import DiscoveredNeighbor
from app.models.snapshot import ConfigSnapshot
from app.services import gns3_service, risk_engine, snapshot_service

if TYPE_CHECKING:
    from app.models.topology_snapshot import TopologySnapshot


@dataclass
class TopologyNode:
    id: str
    hostname: str
    ip_address: str
    vendor: str
    site: str | None
    device_type: str | None
    status: str
    flagged_unstable: bool
    has_config_on_file: bool
    health_color: str | None = None
    health_score: int | None = None
    data_center: str | None = None
    rack: str | None = None
    # Compliance/topology role ("core" | "distribution" | "access" | ... or
    # None if never assigned) -- mirrors Device.device_role, powers the
    # Topology page's optional layered (core/distribution/access) layout.
    device_role: str | None = None
    # Interface errors seen on the device's most recent SNMP poll (delta
    # since the prior poll -- see DeviceMetric.interface_errors), used to
    # badge nodes with recent error activity directly on the Topology map.
    # None if the device has never been polled.
    interface_error_rate: int | None = None


@dataclass
class TopologyEdge:
    source: str  # device id
    target: str  # device id
    subnet: str | None  # e.g. "10.0.12.0/30" -- the shared subnet that ties them together (None for lldp/cdp/gns3 edges)
    source_ip: str | None
    target_ip: str | None
    link_source: str = "subnet"  # "lldp" | "cdp" | "gns3" | "subnet" -- how this edge was inferred
    local_port: str | None = None  # source device's port, if known (lldp/cdp/gns3 only)
    neighbor_port: str | None = None  # target device's port, as reported by the source device
    # Best-effort link utilization for coloring, 0-100 or None if neither
    # endpoint has a recent SNMP poll. This is the *device's* aggregate
    # interface_utilization_pct (see app.core.vm_client),
    # not a true per-port figure -- NetGuard doesn't persist per-ifIndex
    # utilization today, only the whole-device figure computed from
    # whichever interface counters metrics_service last polled. We take
    # the higher of the two endpoints' readings (the busier side is the
    # one that would actually bottleneck this link), which is a reasonable
    # stand-in until per-interface octet counters are tracked per edge.
    utilization_pct: int | None = None


@dataclass
class TopologyGraph:
    nodes: list[TopologyNode] = field(default_factory=list)
    edges: list[TopologyEdge] = field(default_factory=list)


@dataclass
class BlastRadiusResult:
    """Pre-deployment "blast radius" preview (touches X devices, Y of them
    core, Z devices depend on them via topology) -- built from the same
    graph the Topology page renders, so what a reviewer sees before
    approving a change matches what they'd see if they went and looked at
    the topology themselves.

    `touched` = the change's direct targets (device_id + additional_device_ids).
    `dependent` = every other device reachable from a touched device by
    walking topology edges -- i.e. everything that could lose connectivity
    or be otherwise affected if the touched devices misbehave after this
    change, not just the devices being pushed to directly.
    """

    touched_device_ids: list[str] = field(default_factory=list)
    touched_count: int = 0
    touched_core_count: int = 0
    touched_roles: dict[str, int] = field(default_factory=dict)
    dependent_device_ids: list[str] = field(default_factory=list)
    dependent_count: int = 0
    unknown_device_ids: list[str] = field(default_factory=list)  # requested but not found in inventory


def uplink_interfaces_for_device(db: Session, device_id) -> set[str]:
    """The set of local interface names on `device_id` that carry a
    confirmed (LLDP/CDP-discovered) link to another device -- i.e. its
    "uplinks"/inter-device links, as opposed to access ports, loopbacks,
    or unconnected interfaces. Used by validation_engine's pre-push safety
    check to flag a proposed `shutdown` on an interface that's actually
    carrying live topology, not just a guess based on naming convention.

    Best-effort: only reflects links NetGuard has actually discovered (see
    app.api.devices.discover_device). An interface that's a real uplink
    but hasn't been discovered yet simply isn't flagged -- same
    "degrade gracefully, never invent inventory we don't have" posture as
    validation_engine's other cross-checks.
    """
    rows = (
        db.query(DiscoveredNeighbor.local_port)
        .filter(
            DiscoveredNeighbor.device_id == device_id,
            DiscoveredNeighbor.neighbor_device_id.isnot(None),
            DiscoveredNeighbor.local_port.isnot(None),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows if r[0]}


def compute_blast_radius(db: Session, target_device_ids: list[str]) -> BlastRadiusResult:
    """Builds the live topology graph and walks it outward (BFS) from
    `target_device_ids` to find every device that transitively depends on
    them via a discovered/inferred link -- the "N devices depend on them"
    half of the blast-radius preview. `target_device_ids` themselves are
    reported separately as `touched`, not folded into `dependent`, so the
    UI can show "touches 14, 3 core" and "212 more depend on them" as two
    distinct numbers rather than one conflated count.
    """
    graph = build_topology(db)
    nodes_by_id = {n.id: n for n in graph.nodes}

    adjacency: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in adjacency and edge.target in adjacency:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)

    target_ids = {str(t) for t in target_device_ids}
    touched_ids = [t for t in target_ids if t in nodes_by_id]
    unknown_ids = [t for t in target_ids if t not in nodes_by_id]

    # BFS from every touched device simultaneously, collecting anything
    # reachable that isn't itself one of the touched devices.
    visited: set[str] = set(touched_ids)
    dependent: set[str] = set()
    frontier = list(touched_ids)
    while frontier:
        next_frontier = []
        for device_id in frontier:
            for neighbor in adjacency.get(device_id, ()):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                dependent.add(neighbor)
                next_frontier.append(neighbor)
        frontier = next_frontier

    touched_roles: dict[str, int] = {}
    for device_id in touched_ids:
        role = nodes_by_id[device_id].device_role or "unassigned"
        touched_roles[role] = touched_roles.get(role, 0) + 1

    return BlastRadiusResult(
        touched_device_ids=touched_ids,
        touched_count=len(touched_ids),
        touched_core_count=touched_roles.get("core", 0),
        touched_roles=touched_roles,
        dependent_device_ids=sorted(dependent),
        dependent_count=len(dependent),
        unknown_device_ids=unknown_ids,
    )


def _latest_config(db: Session, device_id) -> str | None:
    snap = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.seq.desc())
        .first()
    )
    if not snap:
        return None
    try:
        return snapshot_service.decrypt_config(snap.running_config_encrypted)
    except Exception:
        return None


def _interface_networks(config_text: str) -> list[ipaddress.IPv4Network]:
    """Every interface subnet declared in a config, as normalized
    IPv4Network objects (host bits masked off) so two devices whose
    interfaces sit in the same subnet compare equal regardless of which
    host address either one used.
    """
    parsed = risk_engine.parse_config(config_text)
    networks = []
    for ip, mask in parsed.ip_addresses:
        try:
            networks.append(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False))
        except (ValueError, ipaddress.AddressValueError):
            continue
    return networks


def _latest_metrics_by_device(devices: list[Device]) -> dict[str, dict]:
    """device_id -> its most recent VictoriaMetrics sample (full dict, not
    just health), so callers can pull health_color/health_score *and*
    interface_utilization_pct off one lookup instead of two near-identical
    ones. One vm_client call per device -- fine at prototype fleet sizes
    (same O(devices) tradeoff build_topology already accepts below); a
    device the map has never polled simply isn't in the returned dict
    (caller treats that as unknown/gray, not an error).
    """
    out: dict[str, dict] = {}
    for device in devices:
        latest = vm_client.latest_device_metrics(device.id)
        if latest is not None:
            out[str(device.id)] = latest
    return out


def build_topology(db: Session) -> TopologyGraph:
    """Builds the fleet-wide topology graph: one node per device, edges
    inferred from shared interface subnets across each device's latest
    config snapshot. O(devices^2 * interfaces) -- fine at prototype fleet
    sizes; revisit (e.g. index by subnet first) if this becomes hot.
    """
    devices = db.query(Device).order_by(Device.hostname).all()
    metrics_by_device = _latest_metrics_by_device(devices)

    nodes: list[TopologyNode] = []
    # device_id -> list of (network, original_ip) so edges can report which
    # actual interface IP on each side ties the link together
    device_networks: dict[str, list[tuple[ipaddress.IPv4Network, str]]] = {}

    for device in devices:
        config_text = _latest_config(db, device.id)
        metric = metrics_by_device.get(str(device.id))
        health_color = metric.get("health_color") if metric else None
        health_score = metric.get("health_score") if metric else None
        interface_error_rate = metric.get("interface_errors") if metric else None
        nodes.append(
            TopologyNode(
                id=str(device.id),
                hostname=device.hostname,
                ip_address=device.ip_address,
                vendor=device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
                site=device.site,
                device_type=device.device_type,
                status=device.status.value if hasattr(device.status, "value") else str(device.status),
                flagged_unstable=bool(device.flagged_unstable),
                has_config_on_file=config_text is not None,
                health_color=health_color,
                health_score=health_score,
                data_center=device.data_center,
                rack=device.rack,
                device_role=device.device_role,
                interface_error_rate=interface_error_rate,
            )
        )
        if config_text:
            parsed = risk_engine.parse_config(config_text)
            entries = []
            for ip, mask in parsed.ip_addresses:
                try:
                    net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                except (ValueError, ipaddress.AddressValueError):
                    continue
                entries.append((net, ip))
            device_networks[str(device.id)] = entries

    edges: list[TopologyEdge] = []
    seen_pairs: set[tuple[str, str, str]] = set()  # (device_a, device_b, subnet) so a shared /24 with
    # several matching interface pairs doesn't produce duplicate parallel edges
    device_ids = list(device_networks.keys())
    for i, a_id in enumerate(device_ids):
        for b_id in device_ids[i + 1 :]:
            for net_a, ip_a in device_networks[a_id]:
                for net_b, ip_b in device_networks[b_id]:
                    if net_a != net_b:
                        continue
                    key = (a_id, b_id, str(net_a))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    edges.append(
                        TopologyEdge(source=a_id, target=b_id, subnet=str(net_a), source_ip=ip_a, target_ip=ip_b)
                    )

    # Real LLDP/CDP-confirmed edges from persisted discovery runs (see
    # app.api.devices.discover_device / _persist_discovered_neighbors).
    # These are ground-truth adjacency, unlike the subnet-overlap guess
    # above -- they catch links the subnet heuristic can't see at all
    # (trunked L2-only links, unnumbered point-to-point interfaces,
    # devices without a matching IP on the wire).
    confirmed_pairs: set[tuple[str, str]] = set()
    neighbor_rows = (
        db.query(DiscoveredNeighbor)
        .filter(DiscoveredNeighbor.neighbor_device_id.isnot(None))
        .all()
    )
    for row in neighbor_rows:
        a_id, b_id = str(row.device_id), str(row.neighbor_device_id)
        if a_id == b_id:
            continue
        pair_key = tuple(sorted((a_id, b_id)))
        if pair_key in confirmed_pairs:
            continue
        confirmed_pairs.add(pair_key)
        edges.append(
            TopologyEdge(
                source=a_id,
                target=b_id,
                subnet=None,
                source_ip=None,
                target_ip=None,
                link_source=row.protocol,
                local_port=row.local_port,
                neighbor_port=row.neighbor_port,
            )
        )

    # Drop any subnet-guessed edge that a confirmed LLDP/CDP edge already
    # covers for the same device pair -- real data wins, no need to show
    # the same link twice with different provenance.
    edges = [
        e
        for e in edges
        if e.link_source != "subnet" or tuple(sorted((e.source, e.target))) not in confirmed_pairs
    ]

    # Imported GNS3 lab link topology: for any device that came from a GNS3
    # project (Device.gns3_project_id/gns3_node_id set), pull the project's
    # actual cabling and treat each link as a confirmed edge -- GNS3 knows
    # its own topology exactly, so this is just as trustworthy as LLDP/CDP,
    # and it fills in for lab nodes that haven't (or structurally can't --
    # e.g. an unconfigured/freshly-booted image) run SNMP Discovery yet.
    for edge in _gns3_edges(db, devices, confirmed_pairs):
        edges.append(edge)
        confirmed_pairs.add(tuple(sorted((edge.source, edge.target))))

    # Re-run the same "real data wins" de-dup now that GNS3 edges may have
    # confirmed additional pairs the LLDP/CDP pass didn't know about yet.
    edges = [
        e
        for e in edges
        if e.link_source != "subnet" or tuple(sorted((e.source, e.target))) not in confirmed_pairs
    ]

    # Stamp every edge with a best-effort utilization figure for the
    # Topology page's "color links by utilization" toggle -- see
    # TopologyEdge.utilization_pct's docstring for why this is the higher
    # of the two endpoints' whole-device figures rather than a true
    # per-port number.
    for edge in edges:
        src_metric = metrics_by_device.get(edge.source)
        tgt_metric = metrics_by_device.get(edge.target)
        readings = [
            m.get("interface_utilization_pct")
            for m in (src_metric, tgt_metric)
            if m is not None and m.get("interface_utilization_pct") is not None
        ]
        edge.utilization_pct = round(max(readings)) if readings else None

    return TopologyGraph(nodes=nodes, edges=edges)


def _gns3_edges(
    db: Session,
    devices: list[Device],
    already_confirmed: set[tuple[str, str]],
) -> list[TopologyEdge]:
    """Best-effort: groups GNS3-backed devices by project, fetches each
    project's link list once, and maps GNS3 node_id pairs back to Device
    rows via gns3_node_id. Returns [] (never raises) if GNS3 integration is
    disabled, the controller is unreachable, or a project has since been
    deleted on the controller side -- a Topology page that can't reach
    GNS3 should still render everything it *does* know (LLDP/CDP/subnet),
    not fail outright, same tolerant pattern as the rest of the app's GNS3
    integration.
    """
    by_project: dict[str, dict[str, Device]] = {}
    for d in devices:
        if d.gns3_project_id and d.gns3_node_id:
            by_project.setdefault(d.gns3_project_id, {})[d.gns3_node_id] = d

    edges: list[TopologyEdge] = []
    for project_id, node_map in by_project.items():
        try:
            links = gns3_service.list_links(project_id)
        except gns3_service.GNS3Error:
            continue  # controller unreachable / project gone -- skip this project, not the whole graph

        for link in links:
            nodes = link.get("nodes") or []
            if len(nodes) != 2:
                continue  # not a simple point-to-point cable (or malformed) -- skip
            (node_a, node_b) = nodes
            device_a = node_map.get(node_a.get("node_id"))
            device_b = node_map.get(node_b.get("node_id"))
            if not device_a or not device_b or device_a.id == device_b.id:
                continue  # neighbor is outside our inventory, or not GNS3-mapped, or a self-link

            a_id, b_id = str(device_a.id), str(device_b.id)
            pair_key = tuple(sorted((a_id, b_id)))
            if pair_key in already_confirmed:
                continue  # LLDP/CDP already confirmed this exact pair -- no need for a parallel edge

            edges.append(
                TopologyEdge(
                    source=a_id,
                    target=b_id,
                    subnet=None,
                    source_ip=None,
                    target_ip=None,
                    link_source="gns3",
                    local_port=_gns3_port_label(node_a),
                    neighbor_port=_gns3_port_label(node_b),
                )
            )
            already_confirmed.add(pair_key)

    return edges


def _gns3_port_label(node_link_entry: dict) -> str | None:
    """A link endpoint's port, as GNS3 reports it: prefers the human label
    text (e.g. "Ethernet0/1") if the topology has one set, else falls back
    to "adapter/port" numbers, which every link endpoint always has."""
    label = (node_link_entry.get("label") or {}).get("text")
    if label:
        return str(label)
    adapter = node_link_entry.get("adapter_number")
    port = node_link_entry.get("port_number")
    if adapter is not None and port is not None:
        return f"a{adapter}/{port}"
    return None

# --- Historical snapshots & diffing ("what changed since <period>") ------
#
# build_topology() above is always a live snapshot -- it has no memory of
# what the graph looked like yesterday or last week. capture_snapshot()
# persists a compact copy of the current graph (app.models.
# topology_snapshot.TopologySnapshot); diff_snapshots() compares two of
# them and reports added/removed nodes and edges, the same "what changed"
# framing app.services.drift_service already provides for config content.


def capture_snapshot(db: Session) -> TopologySnapshot:
    import json

    from app.models.topology_snapshot import TopologySnapshot

    graph = build_topology(db)
    nodes = [{"id": n.id, "hostname": n.hostname, "ip_address": n.ip_address} for n in graph.nodes]
    edges = [
        {"source": e.source, "target": e.target, "link_source": e.link_source}
        for e in graph.edges
    ]
    snapshot = TopologySnapshot(
        nodes_json=json.dumps(nodes),
        edges_json=json.dumps(edges),
        node_count=len(nodes),
        edge_count=len(edges),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _edge_key(e: dict) -> tuple[str, str]:
    # Undirected: a link discovered as A->B one day and B->A the next
    # (order can flip depending on which side answered LLDP/CDP first)
    # is the same physical link, not a removal + addition.
    return tuple(sorted((e["source"], e["target"])))


def diff_snapshots(older: TopologySnapshot, newer: TopologySnapshot) -> dict:
    """Returns added/removed nodes and edges between two snapshots
    (older -> newer). Node/edge identity is by device id, so a hostname
    rename on the same device shows as neither an add nor a remove.
    """
    import json

    older_nodes = {n["id"]: n for n in json.loads(older.nodes_json)}
    newer_nodes = {n["id"]: n for n in json.loads(newer.nodes_json)}
    older_edges = {_edge_key(e): e for e in json.loads(older.edges_json)}
    newer_edges = {_edge_key(e): e for e in json.loads(newer.edges_json)}

    added_nodes = [newer_nodes[i] for i in newer_nodes.keys() - older_nodes.keys()]
    removed_nodes = [older_nodes[i] for i in older_nodes.keys() - newer_nodes.keys()]
    added_edges = [newer_edges[k] for k in newer_edges.keys() - older_edges.keys()]
    removed_edges = [older_edges[k] for k in older_edges.keys() - newer_edges.keys()]

    return {
        "older_snapshot_id": str(older.id),
        "newer_snapshot_id": str(newer.id),
        "older_captured_at": older.captured_at.isoformat() if older.captured_at else None,
        "newer_captured_at": newer.captured_at.isoformat() if newer.captured_at else None,
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "unchanged_node_count": len(newer_nodes.keys() & older_nodes.keys()),
        "unchanged_edge_count": len(newer_edges.keys() & older_edges.keys()),
    }
