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

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.discovered_neighbor import DiscoveredNeighbor
from app.models.snapshot import ConfigSnapshot
from app.services import gns3_service, risk_engine, snapshot_service


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


@dataclass
class TopologyGraph:
    nodes: list[TopologyNode] = field(default_factory=list)
    edges: list[TopologyEdge] = field(default_factory=list)


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


def build_topology(db: Session) -> TopologyGraph:
    """Builds the fleet-wide topology graph: one node per device, edges
    inferred from shared interface subnets across each device's latest
    config snapshot. O(devices^2 * interfaces) -- fine at prototype fleet
    sizes; revisit (e.g. index by subnet first) if this becomes hot.
    """
    devices = db.query(Device).order_by(Device.hostname).all()

    nodes: list[TopologyNode] = []
    # device_id -> list of (network, original_ip) so edges can report which
    # actual interface IP on each side ties the link together
    device_networks: dict[str, list[tuple[ipaddress.IPv4Network, str]]] = {}

    for device in devices:
        config_text = _latest_config(db, device.id)
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