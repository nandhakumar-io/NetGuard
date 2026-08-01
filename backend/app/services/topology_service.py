"""Network Topology view.

NetGuard has no CDP/LLDP-style neighbor discovery (no protocol_manager
"show cdp neighbors" read, no separate interface-inventory table) -- what
it *does* have is every device's latest config snapshot, already parsed
for interface IP addresses by the risk engine (`app.services.risk_engine.
parse_config`, used today for duplicate-IP / VLAN-conflict detection).

This reuses that exact same parsing rather than inventing a second config
parser: two devices that each have an interface configured into the same
subnet are, by construction, on the same L2/L3 segment -- i.e. adjacent.
That's a real, data-grounded inference (the same logic that already
crosses devices for duplicate-IP checks), not a guess based on hostname
naming or site grouping.

Devices with no snapshot on file yet still appear as nodes (inventory-only,
no edges) rather than being dropped, so the graph always reflects the full
fleet.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.services import risk_engine, snapshot_service


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
    subnet: str  # e.g. "10.0.12.0/30" -- the shared subnet that ties them together
    source_ip: str
    target_ip: str


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

    return TopologyGraph(nodes=nodes, edges=edges)