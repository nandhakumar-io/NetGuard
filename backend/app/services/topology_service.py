"""Network Topology view.

Edges are built from four sources, in order of trust:

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
     device's latest config snapshot) for devices with a config on file --
     two devices that each have an interface configured into the same
     subnet are, by construction, on the same L2/L3 segment.
  4. Shared-IPAM-subnet inference on management IPs (app.models.subnet)
     as a last-resort fallback for devices with none of the above --
     e.g. freshly added, reachable/online devices that haven't had SNMP
     Discovery run yet and have no config backup on file. If two devices'
     *management* IPs both fall inside the same operator-defined IPAM
     subnet, they're presumed to share that segment. Weakest signal of
     the four (a shared management VLAN doesn't guarantee a data-plane
     adjacency), so it only fires for a device pair with no stronger
     evidence, and is labeled distinctly in the UI so it's never mistaken
     for a confirmed link.

Devices with no snapshot on file yet still appear as nodes (inventory-only,
no edges) rather than being dropped, so the graph always reflects the full
fleet.
"""
from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core import vm_client
from app.models.alert import Alert, AlertSeverity, AlertSource
from app.models.device import Device
from app.models.discovered_neighbor import DiscoveredNeighbor
from app.models.snapshot import ConfigSnapshot
from app.models.subnet import Subnet
from app.models.tenant import Tenant
from app.services import gns3_service, risk_engine, snapshot_service
from app.services.snmp_service import walk_interface_duplex, walk_switchport_vlans

if TYPE_CHECKING:
    from app.models.topology_snapshot import TopologySnapshot

# A confirmed (LLDP/CDP) link whose discovery run is older than this is
# flagged `stale` on the Topology page rather than trusted at face value --
# the device may have rebooted, been recabled, or changed neighbors since,
# and a fresh SNMP Discovery run is the only way to know for sure. Matches
# the cadence discovery is expected to run on (roughly daily); a week of
# silence is well past "just hasn't polled yet."
LINK_STALE_AFTER_DAYS = 7

# Severity ranking so a device with multiple active alerts reports its
# worst one on the topology map (a device with a critical AND a warning
# alert should badge as critical, not whichever row happened to sort last).
_ALERT_SEVERITY_RANK = {
    AlertSeverity.CRITICAL: 3,
    AlertSeverity.WARNING: 2,
    AlertSeverity.INFO: 1,
}


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
    # Worst active (unresolved, non-suppressed) alert severity for this
    # device, or None if it has none -- powers the Topology page's "alert
    # overlay" toggle (red pulse on links touching a device with an
    # active critical alert). See _ALERT_SEVERITY_RANK.
    active_alert_severity: str | None = None
    # Mirrors Device.is_uplink -- lets the Topology page draw WAN/uplink
    # devices distinctly (not just escalate alerts on them, see
    # raise_topology_change_alert below) so an operator can spot the
    # uplink boundary at a glance instead of having to know hostnames.
    is_uplink: bool = False
    # True when this device is the only connection between part of the
    # graph and the rest of it -- i.e. a graph articulation point (see
    # find_single_points_of_failure). Powers the Topology page's "single
    # point of failure" badge, computed proactively for every node
    # instead of only surfacing on-demand via compute_blast_radius.
    is_spof: bool = False
    # Identifies which tenant this node belongs to, if any.
    tenant_name: str | None = None


@dataclass
class TopologyEdge:
    source: str  # device id
    target: str  # device id
    subnet: str | None  # e.g. "10.0.12.0/30" -- the shared subnet that ties them together (None for lldp/cdp/gns3 edges)
    source_ip: str | None
    target_ip: str | None
    link_source: str = "subnet"  # "lldp" | "cdp" | "gns3" | "subnet" | "mgmt_subnet" -- how this edge was inferred
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
    # Derived, coarse real-time state for the Topology page's live-link
    # rendering: "flowing" (up + utilization_pct above IDLE_UTILIZATION_PCT_THRESHOLD),
    # "idle" (up but at/under the threshold, incl. 0%), "down", or
    # "unknown" (no utilization data to classify from -- e.g. a
    # subnet-inferred edge with no recent poll on either endpoint). See
    # _traffic_state.
    traffic_state: str = "unknown"
    # When this edge was last confirmed by an LLDP/CDP discovery run (ISO
    # timestamp), or None for subnet-inferred/GNS3 edges where "confirmed"
    # doesn't apply the same way. Powers the "live vs. inferred" link-age
    # display -- a link's neighbor data only reflects reality as of this
    # timestamp, and a rebooted/recabled device won't show up as changed
    # until the next discovery run overwrites it.
    last_confirmed_at: str | None = None
    # True when an lldp/cdp edge's discovery run is older than
    # LINK_STALE_AFTER_DAYS -- flagged in the UI so a stale-but-still-drawn
    # link isn't mistaken for a just-confirmed one.
    stale: bool = False
    # True when either endpoint device is flagged Device.is_uplink --
    # lets the Topology page render uplink-touching links distinctly
    # (thicker/different color), same signal raise_topology_change_alert
    # already escalates on when a link like this disappears.
    is_uplink: bool = False
    # Bundle members: when two devices are joined by more than one
    # confirmed (LLDP/CDP/GNS3) physical link -- e.g. a real LACP/
    # port-channel trunk -- every individual local_port/neighbor_port
    # pair used to collapse into a single edge (first-seen-wins), which
    # silently hid the other members of the bundle from the map. Now
    # every confirmed link between the same device pair is kept as one
    # `LinkMember` here, and `local_port`/`neighbor_port` above just
    # mirror the first member for callers that don't care about bundles.
    # Empty for subnet/mgmt_subnet-inferred edges, which have no
    # per-port data at all.
    members: list["LinkMember"] = field(default_factory=list)
    # True when at least one member has a confirmed half/full duplex
    # mismatch between its two ends (see LinkMember.duplex_mismatch) --
    # rolled up here so the Topology page can badge the link itself
    # without a caller having to walk every member.
    duplex_mismatch: bool = False
    # True when at least one member has a confirmed VLAN trunk
    # allowed-list mismatch between its two ends (see
    # LinkMember.vlan_mismatch) -- rolled up here the same way
    # duplex_mismatch is, so the Topology page can badge the edge
    # itself without a caller having to walk every member.
    vlan_mismatch: bool = False


@dataclass
class LinkMember:
    local_port: str | None
    neighbor_port: str | None
    protocol: str  # "lldp" | "cdp" | "gns3"
    last_confirmed_at: str | None
    stale: bool
    # Per-member operational state, best-effort from the source device's
    # latest interface poll (app.core.vm_client interface list) matched
    # by port name -- "up" / "down" / "unknown" (no recent poll data for
    # that interface). Lets the UI show *which* member of a trunk is
    # actually forwarding vs. one that's cabled but administratively/
    # operationally down, instead of a single link-wide status.
    status: str = "unknown"
    utilization_pct: int | None = None
    # Best-effort switchport mode for this member's local_port ("trunk" |
    # "access" | "routed" | None if unresolved), plus the VLAN(s) it
    # carries -- see _member_switchport_info. Powers the Topology page's
    # "is this an access or trunk link" display, which previously had no
    # way to show anything but the raw LLDP/CDP port pair.
    port_mode: str | None = None
    vlan: str | None = None
    trunk_vlans: list[str] | None = None
    # Same coarse classification as TopologyEdge.traffic_state, computed
    # per-member from its own status/utilization_pct rather than the
    # edge's aggregate -- lets the UI animate individual trunk members
    # independently (one member of a port-channel can be idle while
    # another is saturated).
    traffic_state: str = "unknown"
    # Best-effort duplex mode read off *this member's* local_port via
    # EtherLike-MIB (snmp_service.walk_interface_duplex) -- "half" |
    # "full" | "unknown" | None (unresolved: no SNMP, or the platform
    # doesn't expose dot3StatsDuplexStatus for that port).
    local_duplex: str | None = None
    # Same read taken from the *neighbor* device's own SNMP session at
    # neighbor_port -- i.e. what the other end of this exact cable
    # thinks its duplex is set to. Kept alongside local_duplex (rather
    # than just a bool) so the Topology page's link-detail panel can
    # show "this end: full / that end: half" instead of only a flag.
    neighbor_duplex: str | None = None
    # True only when both ends resolved to a real, differing duplex
    # setting (half vs full) -- never fabricated from a missing or
    # "unknown" reading on either side, same "don't invent data we
    # don't have" posture as the rest of this module. A classic silent
    # cause of packet loss/retransmits that never trips an
    # interface-down alert, so it's worth flagging on the link itself.
    duplex_mismatch: bool = False
    # True when both ends of this member are confirmed trunk ports (see
    # port_mode above) but their allowed/trunk VLAN sets don't match --
    # e.g. VLAN 40 is defined and trunked on this side but missing from
    # the neighbor's trunk allowed-list on the other end of the exact
    # same cable. A very common self-inflicted outage cause on stacked/
    # redundant switch pairs: the link stays up, LLDP/CDP still confirms
    # it, but traffic on the missing VLAN silently black-holes at this
    # boundary. Never set when either side's mode/trunk_vlans couldn't
    # be resolved -- same "don't invent data we don't have" posture as
    # duplex_mismatch above.
    vlan_mismatch: bool = False
    # The VLAN IDs actually responsible for the mismatch -- i.e. the
    # symmetric difference between the two ends' trunk_vlans -- so the
    # UI/alert message can name the exact VLAN(s) instead of just
    # flagging "something doesn't match". None whenever vlan_mismatch
    # is False.
    vlan_mismatch_vlans: list[str] | None = None


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


# Below this, a link is considered idle rather than actively passing
# meaningful traffic -- low enough to still show a live-but-quiet link
# distinctly from one saturated or trending toward it, high enough that
# background/control-plane chatter on an otherwise-quiet port doesn't
# read as "flowing".
IDLE_UTILIZATION_PCT_THRESHOLD = 2


def _traffic_state(status: str, utilization_pct: int | None) -> str:
    """Coarse real-time classification for the Topology page's live link
    rendering. Never fabricates a state from missing data -- "unknown"
    for anything without a recent enough poll to say otherwise, same
    "don't invent data we don't have" posture as the rest of this module.
    """
    if status == "down":
        return "down"
    if status != "up" or utilization_pct is None:
        return "unknown"
    return "flowing" if utilization_pct > IDLE_UTILIZATION_PCT_THRESHOLD else "idle"


def _member_port_state(db: Session, device_id: str, local_port: str | None) -> tuple[str, int | None]:
    """Best-effort per-port operational state for one trunk member,
    matched by interface name against the two things NetGuard actually
    tracks per-port: an active unresolved "Interface Down: <port>" alert
    (see app.services.metrics_service), and the latest polled utilization
    for that ifIndex (app.core.vm_client.latest_interface_metrics).

    Returns ("down" | "up" | "unknown", utilization_pct-or-None). "unknown"
    means neither source has anything for that port name yet -- cabled per
    LLDP/CDP but never independently polled, which the UI should render as
    "cabled" rather than implying it's actively passing traffic.
    """
    if not local_port:
        return "unknown", None
    try:
        down_alert = (
            db.query(Alert.id)
            .filter(
                Alert.device_id == device_id,
                Alert.category == f"Interface Down: {local_port}",
                Alert.resolved.is_(False),
            )
            .first()
        )
        if down_alert:
            return "down", None
        for row in vm_client.latest_interface_metrics(device_id):
            if row.get("if_descr") == local_port:
                util = row.get("utilization_pct")
                return "up", int(util) if util is not None else None
    except Exception:
        return "unknown", None
    return "unknown", None


def _member_switchport_info(
    devices_by_id: dict[str, Device],
    switchport_cache: dict[str, dict],
    device_id: str,
    local_port: str | None,
) -> tuple[str | None, str | None, list[str] | None]:
    """Best-effort switchport mode/VLAN for one trunk member's local_port,
    so the Topology page can badge a confirmed link "trunk" or "access"
    instead of only showing the raw LLDP/CDP port pair (see
    app.api.config_management.view_interfaces, which does the same
    per-device SNMP walk for the device detail Interfaces tab -- this is
    the same data source, just reused here and cached per device_id so a
    trunk with several members doesn't re-walk the same device's VLAN
    table once per member).

    SNMP-only: unlike the device Interfaces tab, this has no NETCONF
    session already open to a Juniper device to fall back to, so Juniper
    switchport info only resolves here if SNMP is also configured on the
    device. Returns (None, None, None) wherever it can't be resolved --
    never fabricated.
    """
    if not local_port:
        return None, None, None
    device = devices_by_id.get(device_id)
    if not device or not device.snmp_version:
        return None, None, None
    # Skip the live walk entirely for a device we already know is
    # unreachable -- otherwise every confirmed link touching an
    # offline/degraded device pays the full SNMP timeout+retry cost
    # (walk_switchport_vlans alone issues 4 sequential walks, each up to
    # timeout*(retries+1) seconds) on every single Topology page load.
    # This is the main reason the page was slow to render on any fleet
    # with even a few unreachable devices. A short timeout is kept below
    # (rather than the 3.0s default) as a second guard for devices that
    # *look* online but have since dropped off, so one flaky device still
    # can't stall the whole graph for long.
    if device.status is not None and str(device.status).lower() not in ("online", "degraded"):
        switchport_cache.setdefault(device_id, {})
    if device_id not in switchport_cache:
        try:
            from app.services import metrics_service

            auth = metrics_service.build_snmp_auth(device)
            switchport_cache[device_id] = walk_switchport_vlans(device.ip_address, auth, timeout=1.0)
        except Exception:
            switchport_cache[device_id] = {}
    info = switchport_cache[device_id].get(local_port)
    if not info:
        return None, None, None
    return info.get("mode"), info.get("vlan"), info.get("trunk_vlans")


def _member_port_duplex(
    devices_by_id: dict[str, Device],
    duplex_cache: dict[str, dict],
    device_id: str,
    port: str | None,
) -> str | None:
    """Best-effort duplex mode for one end of a link, read off
    `device_id`'s own SNMP session at `port` (see
    snmp_service.walk_interface_duplex) and cached per device_id --
    called once per member for the local side and once for the
    neighbor side, so a trunk with several members, or two members
    landing on the same neighbor, don't re-walk the same device's
    EtherLike-MIB table more than once.

    Returns None wherever it can't be resolved (no SNMP configured, no
    port name, walk failed, or the platform doesn't expose duplex for
    that port) -- duplex_mismatch below only ever compares two actually
    -known readings, never a fabricated default.
    """
    if not port:
        return None
    device = devices_by_id.get(device_id)
    if not device or not device.snmp_version:
        return None
    # Same reachability short-circuit and reduced timeout as
    # _member_switchport_info above, and for the same reason: this is a
    # best-effort annotation on top of the topology graph, not part of
    # the graph itself, so it must never be allowed to make the whole
    # page wait on a device that's known (or turns out) to be down.
    if device.status is not None and str(device.status).lower() not in ("online", "degraded"):
        duplex_cache.setdefault(device_id, {})
    if device_id not in duplex_cache:
        try:
            from app.services import metrics_service

            auth = metrics_service.build_snmp_auth(device)
            duplex_cache[device_id] = walk_interface_duplex(device.ip_address, auth, timeout=1.0)
        except Exception:
            duplex_cache[device_id] = {}
    return duplex_cache[device_id].get(port)


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


def find_single_points_of_failure(nodes: list[TopologyNode], edges: list[TopologyEdge]) -> set[str]:
    """Graph articulation points: devices whose removal would split the
    rest of the (currently connected-through-them) graph into more than
    one piece -- i.e. "no redundant path" nodes where losing that one
    device doesn't just take itself offline, it cuts other devices off
    from each other too. Built on the same adjacency structure
    compute_blast_radius() builds from build_topology()'s graph, just
    walked with the standard iterative low-link DFS instead of a BFS
    from a target set, so it can run proactively for every node instead
    of only on-demand for an operator-chosen change target.

    A leaf node (degree <= 1) is never a SPOF by this definition -- it
    has nothing behind it that would be cut off, only itself, which
    compute_blast_radius already reports via `touched` rather than
    `dependent`. Isolated/degree-0 nodes are likewise excluded.
    """
    adjacency: dict[str, set[str]] = {n.id: set() for n in nodes}
    for edge in edges:
        if edge.source in adjacency and edge.target in adjacency and edge.source != edge.target:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)

    discovery: dict[str, int] = {}
    low: dict[str, int] = {}
    articulation: set[str] = set()
    timer = 0

    for start in adjacency:
        if start in discovery:
            continue
        # Iterative DFS: stack of (node, parent, iterator-over-neighbors)
        root_children = 0
        stack: list[tuple[str, str | None, iter]] = [(start, None, iter(adjacency[start]))]
        discovery[start] = low[start] = timer
        timer += 1

        while stack:
            node, parent, neighbors = stack[-1]
            advanced = False
            for neighbor in neighbors:
                if neighbor == parent:
                    continue
                if neighbor in discovery:
                    low[node] = min(low[node], discovery[neighbor])
                else:
                    discovery[neighbor] = low[neighbor] = timer
                    timer += 1
                    if node == start:
                        root_children += 1
                    stack.append((neighbor, node, iter(adjacency[neighbor])))
                    advanced = True
                break
            if advanced:
                continue

            stack.pop()
            if stack:
                gp_node = stack[-1][0]
                low[gp_node] = min(low[gp_node], low[node])
                if gp_node != start and low[node] >= discovery[gp_node]:
                    articulation.add(gp_node)

        if root_children > 1:
            articulation.add(start)

    return articulation


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
    just health), keyed by str(device.id), so callers can pull health_color/
    health_score *and* interface_utilization_pct off one lookup instead of
    two near-identical ones.

    Previously this looped over every device and called
    vm_client.latest_device_metrics(device.id) once per device -- N
    serialized HTTP round-trips to VictoriaMetrics for an N-device fleet
    (every one is a separate /api/v1/query call). fleet_latest_metrics()
    fetches the same data for ALL devices with exactly two flat PromQL
    queries (one for scalar gauges, one for info labels), regardless of
    fleet size -- same O(constant) tradeoff fleet_health_summary() already
    uses for its fleet_latest_health(). A device the map has never polled
    simply isn't in the returned dict (caller treats that as unknown/gray).
    """
    fleet = vm_client.fleet_latest_metrics()
    # fleet_latest_metrics() keys by uuid.UUID; callers here use str(device.id)
    return {str(dev_id): row for dev_id, row in fleet.items()}


def _active_alert_severity_by_device(db: Session) -> dict[str, str]:
    """device_id -> worst active alert severity (see _ALERT_SEVERITY_RANK),
    for every device with at least one unresolved, non-suppressed alert.
    Suppressed alerts (topology-correlation consequences or maintenance-
    window noise) are excluded so the map's alert overlay highlights root
    causes, not every downstream device a real outage cascaded into.
    """
    rows = (
        db.query(Alert.device_id, Alert.severity)
        .filter(
            Alert.resolved.is_(False),
            Alert.suppressed.is_(False),
            Alert.suppressed_by_window_id.is_(None),
            Alert.device_id.isnot(None),
        )
        .all()
    )
    out: dict[str, str] = {}
    for device_id, severity in rows:
        key = str(device_id)
        current = out.get(key)
        if current is None or _ALERT_SEVERITY_RANK.get(severity, 0) > _ALERT_SEVERITY_RANK.get(
            AlertSeverity(current), 0
        ):
            out[key] = severity.value if hasattr(severity, "value") else str(severity)
    return out


def build_topology(db: Session, tenant_id: uuid.UUID | None = None) -> TopologyGraph:
    """Builds the topology graph: one node per device, edges inferred
    from shared interface subnets across each device's latest config
    snapshot. O(devices^2 * interfaces) -- fine at prototype fleet
    sizes; revisit (e.g. index by subnet first) if this becomes hot.

    `tenant_id=None` returns the fleet-wide graph across every tenant --
    only appropriate for MSP staff (app.core.deps.get_tenant_scope
    returns None for them, same convention as app.api.devices). A
    regular tenant user's tenant_id must always be passed so their
    topology view can't include another customer's devices.
    """
    q = db.query(Device)
    if tenant_id is not None:
        q = q.filter(Device.tenant_id == tenant_id)
    devices = q.order_by(Device.hostname).all()
    devices_by_id = {str(d.id): d for d in devices}
    tenants_by_id = {str(t.id): t.name for t in db.query(Tenant).all()}
    switchport_cache: dict[str, dict] = {}
    duplex_cache: dict[str, dict] = {}
    metrics_by_device = _latest_metrics_by_device(devices)
    alert_severity_by_device = _active_alert_severity_by_device(db)

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
                active_alert_severity=alert_severity_by_device.get(str(device.id)),
                is_uplink=bool(device.is_uplink),
                tenant_name=tenants_by_id.get(str(device.tenant_id)) if device.tenant_id else None,
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
    # Group every confirmed row by device pair first, rather than keeping
    # only the first row seen per pair -- a real LACP/port-channel trunk
    # reports one LLDP/CDP neighbor row *per physical member link*, and
    # collapsing those to a single first-seen-wins edge silently dropped
    # every member but one from the map. Each pair now becomes exactly
    # one TopologyEdge carrying a `members` list, so a 4-cable trunk still
    # renders as one link line (it *is* one logical link) but the Link
    # Detail panel can show and independently badge all 4 members.
    rows_by_pair: dict[tuple[str, str], list[DiscoveredNeighbor]] = {}
    for row in neighbor_rows:
        a_id, b_id = str(row.device_id), str(row.neighbor_device_id)
        if a_id == b_id:
            continue
        pair_key = tuple(sorted((a_id, b_id)))
        rows_by_pair.setdefault(pair_key, []).append(row)

    for pair_key, rows in rows_by_pair.items():
        confirmed_pairs.add(pair_key)
        a_id, b_id = pair_key
        # Preserve each row's own (device_id, neighbor_device_id) order for
        # local_port/neighbor_port so "local" always means "on `source`",
        # even though members are deduped/grouped by the unordered pair.
        first = rows[0]
        source_id, target_id = str(first.device_id), str(first.neighbor_device_id)
        members: list[LinkMember] = []
        # Keyed on the UNORDERED pair of physical endpoints -- i.e.
        # {(this device, this row's local_port), (neighbor, this row's
        # neighbor_port)} -- not on (local_port, neighbor_port) in this
        # row's own order. LLDP is reported from both ends: device A's
        # poll writes a row with local_port=A-side, neighbor_port=B-side,
        # and device B's own poll writes a *separate* row for the exact
        # same cable with local_port=B-side, neighbor_port=A-side. Keying
        # on the ordered tuple treated those as two different members,
        # so every real physical link was counted twice (a genuine
        # 5-member trunk rendered as "LLDP x10") -- keying on the
        # unordered endpoint pair collapses both sides' reports of the
        # same cable back into the one member it actually is.
        def _norm_port(p: str | None) -> str:
            return (p or "").strip().lower()

        def _latest_per_local_port(rs: list[DiscoveredNeighbor]) -> list[DiscoveredNeighbor]:
            # Collapse repeated self-reports of the identical local_port
            # (the same side re-confirming the same cable across separate
            # discovery runs) down to the most recently discovered row.
            by_port: dict[str, DiscoveredNeighbor] = {}
            for r in rs:
                key = _norm_port(r.local_port)
                existing = by_port.get(key)
                if existing is None or (r.discovered_at or datetime.min.replace(tzinfo=timezone.utc)) > (
                    existing.discovered_at or datetime.min.replace(tzinfo=timezone.utc)
                ):
                    by_port[key] = r
            return list(by_port.values())

        rows_a = _latest_per_local_port([r for r in rows if str(r.device_id) == a_id])
        rows_b = _latest_per_local_port([r for r in rows if str(r.device_id) == b_id])
        # Pair each A-side row with its B-side mirror by matching each
        # row's own self-reported local_port against the OTHER row's
        # neighbor_port. This replaces a previous approach that keyed
        # purely on the frozenset of both sides' reports (device, port)
        # pairs, including the *neighbor*-reported port -- which silently
        # failed to dedupe (a genuine N-cable trunk rendered as up to 2N
        # members / "LLDP x12" for 6 real cables) whenever one side's
        # neighbor-port ifIndex->name lookup didn't resolve to the exact
        # same string the neighbor uses for its own local_port (SNMP
        # timeout, vendor formatting difference, etc). A device's report
        # of its OWN local_port is always reliable, so matching on that
        # self-reported value first -- with a fall through to the raw
        # neighbor_port anyway -- correctly collapses both sides' rows
        # for the same cable even when the cross-resolution above failed.
        used_b: set[int] = set()
        deduped_rows: list[DiscoveredNeighbor] = []
        for ra in rows_a:
            ra_lp, ra_np = _norm_port(ra.local_port), _norm_port(ra.neighbor_port)
            match_idx = None
            for i, rb in enumerate(rows_b):
                if i in used_b:
                    continue
                rb_lp, rb_np = _norm_port(rb.local_port), _norm_port(rb.neighbor_port)
                if (ra_np and ra_np == rb_lp) or (rb_np and rb_np == ra_lp):
                    match_idx = i
                    break
            if match_idx is not None:
                used_b.add(match_idx)
                rb = rows_b[match_idx]
                # Either row describes the same physical cable -- keep
                # whichever side confirmed it more recently.
                deduped_rows.append(
                    ra
                    if (ra.discovered_at or datetime.min.replace(tzinfo=timezone.utc))
                    >= (rb.discovered_at or datetime.min.replace(tzinfo=timezone.utc))
                    else rb
                )
            else:
                deduped_rows.append(ra)
        for i, rb in enumerate(rows_b):
            if i not in used_b:
                deduped_rows.append(rb)

        for row in deduped_rows:
            confirmed_at = row.discovered_at
            is_stale = bool(
                confirmed_at
                and datetime.now(timezone.utc) - confirmed_at.replace(tzinfo=confirmed_at.tzinfo or timezone.utc)
                > timedelta(days=LINK_STALE_AFTER_DAYS)
            )
            member_device_id = str(row.device_id)
            member_neighbor_id = str(row.neighbor_device_id)
            status, member_util = _member_port_state(db, member_device_id, row.local_port)
            port_mode, vlan, trunk_vlans = _member_switchport_info(
                devices_by_id, switchport_cache, member_device_id, row.local_port
            )
            local_duplex = _member_port_duplex(devices_by_id, duplex_cache, member_device_id, row.local_port)
            neighbor_duplex = _member_port_duplex(devices_by_id, duplex_cache, member_neighbor_id, row.neighbor_port)
            duplex_mismatch = (
                local_duplex in ("half", "full")
                and neighbor_duplex in ("half", "full")
                and local_duplex != neighbor_duplex
            )
            # VLAN trunk allowed-list consistency: resolve the *neighbor*
            # side's switchport info too (the loop above only ever
            # walked the local side) so both ends of this exact cable
            # can be compared. Only meaningful when both ends are
            # confirmed trunk ports with a known VLAN list -- an access
            # port, a routed port, or a side that couldn't be resolved
            # at all never gets flagged, same "don't invent data we
            # don't have" posture as duplex_mismatch above.
            neighbor_port_mode, _neighbor_vlan, neighbor_trunk_vlans = _member_switchport_info(
                devices_by_id, switchport_cache, member_neighbor_id, row.neighbor_port
            )
            vlan_mismatch = False
            vlan_mismatch_vlans: list[str] | None = None
            if port_mode == "trunk" and neighbor_port_mode == "trunk" and trunk_vlans and neighbor_trunk_vlans:
                diff = set(trunk_vlans) ^ set(neighbor_trunk_vlans)
                if diff:
                    vlan_mismatch = True
                    vlan_mismatch_vlans = sorted(diff, key=lambda v: int(v) if v.isdigit() else 0)
            members.append(
                LinkMember(
                    local_port=row.local_port,
                    neighbor_port=row.neighbor_port,
                    protocol=row.protocol,
                    last_confirmed_at=confirmed_at.isoformat() if confirmed_at else None,
                    stale=is_stale,
                    status=status,
                    utilization_pct=member_util,
                    port_mode=port_mode,
                    vlan=vlan,
                    trunk_vlans=trunk_vlans,
                    traffic_state=_traffic_state(status, member_util),
                    local_duplex=local_duplex,
                    neighbor_duplex=neighbor_duplex,
                    duplex_mismatch=duplex_mismatch,
                    vlan_mismatch=vlan_mismatch,
                    vlan_mismatch_vlans=vlan_mismatch_vlans,
                )
            )
        members.sort(key=lambda m: (m.local_port or "", m.neighbor_port or ""))
        newest_confirmed = max((m.last_confirmed_at for m in members if m.last_confirmed_at), default=None)
        all_stale = all(m.stale for m in members) if members else False
        any_duplex_mismatch = any(m.duplex_mismatch for m in members)
        any_vlan_mismatch = any(m.vlan_mismatch for m in members)
        # Edge-level state rolls up its members: "flowing" if any member
        # is actively passing traffic (the link as a whole is live even
        # if one trunk member is idle), else "down" if every member is
        # down, else "idle" if at least one member has a known up-but-
        # quiet state, else "unknown".
        member_states = {m.traffic_state for m in members}
        if "flowing" in member_states:
            edge_traffic_state = "flowing"
        elif member_states and member_states == {"down"}:
            edge_traffic_state = "down"
        elif "idle" in member_states:
            edge_traffic_state = "idle"
        else:
            edge_traffic_state = "unknown"
        edges.append(
            TopologyEdge(
                source=source_id,
                target=target_id,
                subnet=None,
                source_ip=None,
                target_ip=None,
                link_source=first.protocol,
                local_port=members[0].local_port if members else first.local_port,
                neighbor_port=members[0].neighbor_port if members else first.neighbor_port,
                last_confirmed_at=newest_confirmed,
                stale=all_stale,
                members=members,
                traffic_state=edge_traffic_state,
                duplex_mismatch=any_duplex_mismatch,
                vlan_mismatch=any_vlan_mismatch,
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

    # Tier 4 fallback: shared-IPAM-subnet inference on management IPs, for
    # any device pair that's still edge-less after LLDP/CDP, GNS3, and
    # config-parsed subnet inference all had a shot -- typically a
    # freshly-added, reachable device that hasn't had SNMP Discovery run
    # against it yet and has no config backup on file (so tier 3 has
    # nothing to parse). Without this, such a device shows as a fully
    # isolated node on the map even when it's plainly on the same segment
    # as its neighbors, per the IPAM subnets an operator already defined.
    # See this module's docstring for why this is weakest-signal and
    # labeled `mgmt_subnet` rather than folded into the `subnet` tier.
    strong_pairs: set[tuple[str, str]] = confirmed_pairs | {
        tuple(sorted((e.source, e.target))) for e in edges if e.link_source == "subnet"
    }
    for edge in _mgmt_subnet_edges(db, devices, strong_pairs):
        edges.append(edge)
        strong_pairs.add(tuple(sorted((edge.source, edge.target))))

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
        # Edges with confirmed LLDP/CDP members already got a real,
        # per-port traffic_state rolled up from those members above --
        # only derive it from the device-wide figure here for edges that
        # never went through that path (subnet/mgmt_subnet/gns3-inferred,
        # which have no per-port data).
        if not edge.members:
            src_status = "unknown"
            if src_metric is not None or tgt_metric is not None:
                src_status = "up"  # a device with a recent poll at all is reachable
            edge.traffic_state = _traffic_state(src_status, edge.utilization_pct)

    # Stamp uplink highlighting: a link touching a WAN/uplink-flagged
    # device on either end is itself an uplink link for map styling.
    uplink_node_ids = {n.id for n in nodes if n.is_uplink}
    for edge in edges:
        edge.is_uplink = edge.source in uplink_node_ids or edge.target in uplink_node_ids

    # Stamp "single point of failure" badges: nodes whose removal would
    # split the graph (see find_single_points_of_failure). Computed
    # proactively for every node here rather than only on-demand, so the
    # badge shows up on the map itself without an operator having to run
    # a blast-radius check first.
    spof_ids = find_single_points_of_failure(nodes, edges)
    for node in nodes:
        node.is_spof = node.id in spof_ids

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


def _mgmt_subnet_edges(
    db: Session,
    devices: list[Device],
    already_linked: set[tuple[str, str]],
) -> list[TopologyEdge]:
    """Tier 4 fallback (see module docstring): for every operator-defined
    IPAM subnet, any two managed devices whose *management* IPs both fall
    inside it get an edge -- unless that pair already has a stronger-tier
    edge (LLDP/CDP, GNS3, or config-parsed subnet overlap), or is missing
    an IP/subnet to compare in the first place.

    O(subnets * devices^2) worst case, same "fine at prototype fleet
    sizes" tradeoff the rest of this module accepts. Devices without a
    parseable ip_address, or that fall inside no configured subnet at
    all, simply don't participate -- same tolerant, no-invented-data
    posture as every other inference tier here.
    """
    subnets = db.query(Subnet).all()
    if not subnets:
        return []

    nets: list[ipaddress.IPv4Network] = []
    for s in subnets:
        try:
            nets.append(ipaddress.ip_network(s.cidr, strict=False))
        except ValueError:
            continue

    device_ip: dict[str, ipaddress.IPv4Address] = {}
    for d in devices:
        if not d.ip_address:
            continue
        try:
            device_ip[str(d.id)] = ipaddress.ip_address(d.ip_address)
        except ValueError:
            continue

    device_ids = list(device_ip.keys())
    edges: list[TopologyEdge] = []
    seen_pairs: set[tuple[str, str]] = set()

    for net in nets:
        members = [did for did in device_ids if device_ip[did] in net]
        for i, a_id in enumerate(members):
            for b_id in members[i + 1 :]:
                pair_key = tuple(sorted((a_id, b_id)))
                if pair_key in already_linked or pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                edges.append(
                    TopologyEdge(
                        source=a_id,
                        target=b_id,
                        subnet=str(net),
                        source_ip=str(device_ip[a_id]),
                        target_ip=str(device_ip[b_id]),
                        link_source="mgmt_subnet",
                    )
                )
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


def raise_vlan_consistency_alerts(db: Session, graph: TopologyGraph) -> None:
    """Per-link VLAN trunk allowed-list consistency report: raises one
    device-scoped Alert per confirmed physical link where
    LinkMember.vlan_mismatch is True (see its docstring) -- e.g. VLAN 40
    is trunked on this switch's side of a link but missing from the
    trunk allowed-list on the neighbor's side of the exact same cable.
    A very common self-inflicted outage cause on stacked/redundant
    switch pairs that a plain link-up/down check can never see, since
    the link itself stays up throughout.

    Scoped to (and deduped on) the *local* device + local_port, same as
    every other per-interface alert in this app (e.g. "Interface Down:
    <port>") -- a link has two sides, and each side's operator wants to
    see the mismatch flagged against the box and port they'd actually
    go fix, not just once against an arbitrary "source" endpoint.
    Cleared automatically the next time this runs and the mismatch is
    gone (config fixed, or the link no longer confirmed at all).
    """
    from app.services.alert_service import auto_resolve, raise_alert

    mismatched_ports: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if not edge.vlan_mismatch:
            continue
        for member in edge.members:
            if not member.vlan_mismatch or not member.local_port:
                continue
            vlan_list = ", ".join(member.vlan_mismatch_vlans or [])
            category = f"VLAN Trunk Mismatch: {member.local_port}"
            mismatched_ports.add((edge.source, category))
            raise_alert(
                db,
                device_id=uuid.UUID(edge.source),
                severity=AlertSeverity.WARNING,
                source=AlertSource.HEALTH_POLL,
                category=category,
                message=(
                    f"Trunk allowed-VLAN mismatch on {member.local_port} "
                    f"(neighbor port {member.neighbor_port or 'unknown'}): "
                    f"VLAN(s) {vlan_list} present on one side but not the other."
                ),
            )

    # Auto-resolve any previously-raised mismatch alert for a device/port
    # this pass didn't re-flag -- same "still-active breaches re-fire,
    # fixed ones clear" contract as every other alert source here.
    stale_alerts = (
        db.query(Alert)
        .filter(Alert.category.like("VLAN Trunk Mismatch:%"), Alert.resolved.is_(False))
        .all()
    )
    for alert in stale_alerts:
        key = (str(alert.device_id), alert.category)
        if key not in mismatched_ports:
            auto_resolve(db, device_id=alert.device_id, category=alert.category, note="VLAN trunk mismatch no longer detected")


def capture_snapshot(db: Session) -> TopologySnapshot:
    import json

    from app.models.topology_snapshot import TopologySnapshot

    graph = build_topology(db)

    # VLAN trunk consistency is evaluated off the same graph build as
    # every periodic snapshot capture (see app.main._topology_snapshot_loop)
    # so it's checked on a regular cadence without needing its own
    # separate poll loop. Best-effort: a failure here should never break
    # snapshot capture itself.
    try:
        raise_vlan_consistency_alerts(db, graph)
    except Exception:  # noqa: BLE001
        pass

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


def raise_topology_change_alert(db: Session, older: "TopologySnapshot", newer: "TopologySnapshot") -> None:
    """Raises a fleet-wide (device_id=None) alert whenever the periodic
    snapshot diff finds any actual change -- a device or a confirmed/
    inferred link appearing or disappearing since the last capture.

    Runs after every TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS capture (see
    app.main._topology_snapshot_loop) so a rewired trunk, a device that
    dropped off LLDP, or a newly-cabled neighbor shows up in Alerts /
    the notification bell immediately, not only for someone who happens
    to open the Topology page and click "What changed?".
    """
    from app.models.device import Device
    from app.services.alert_service import raise_alert

    diff = diff_snapshots(older, newer)
    changes: list[str] = []
    if diff["added_nodes"]:
        changes.append(f"{len(diff['added_nodes'])} device(s) added")
    if diff["removed_nodes"]:
        changes.append(f"{len(diff['removed_nodes'])} device(s) removed")
    if diff["added_edges"]:
        changes.append(f"{len(diff['added_edges'])} link(s) added")
    if diff["removed_edges"]:
        changes.append(f"{len(diff['removed_edges'])} link(s) removed")
    if not changes:
        return

    # A device disappearing (or a confirmed link being lost, which is the
    # LLDP/CDP equivalent of a device going dark to that neighbor) is
    # operationally more urgent than something new merely being added.
    # If a WAN/uplink-flagged device (see Device.is_uplink) is one of the
    # devices/link endpoints removed, that's escalated straight to
    # critical -- losing a device on the LAN side is a warning, losing an
    # uplink is a site going dark.
    uplink_ids: set[str] = set()
    if diff["removed_nodes"] or diff["removed_edges"]:
        uplink_ids = {
            str(d.id)
            for d in db.query(Device.id).filter(Device.is_uplink.is_(True)).all()
        }
    removed_node_ids = {n["id"] for n in diff["removed_nodes"]}
    removed_edge_endpoints = {e["source"] for e in diff["removed_edges"]} | {e["target"] for e in diff["removed_edges"]}
    uplink_affected = bool(uplink_ids & (removed_node_ids | removed_edge_endpoints))

    if uplink_affected:
        severity = AlertSeverity.CRITICAL
        changes.append("includes a WAN/uplink device")
    elif diff["removed_nodes"] or diff["removed_edges"]:
        severity = AlertSeverity.WARNING
    else:
        severity = AlertSeverity.INFO
    raise_alert(
        db,
        device_id=None,
        severity=severity,
        source=AlertSource.HEALTH_POLL,
        category="Topology Changed",
        message="Network topology changed: " + ", ".join(changes) + ".",
    )


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
