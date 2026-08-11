"""Pre-deployment impact simulation ("what-if" dry run).

Answers the question a NOC operator actually wants answered before they
click Approve on a change: *if this config goes out, which devices lose
reachability, and is there a redundant path or not* -- not just "does the
CLI syntax parse" (app.services.validation_engine) or "how many devices
does this touch" (app.services.topology_service.compute_blast_radius).

Reuses the exact same topology graph both of those already build
(app.services.topology_service.build_topology, the same one the Topology
page renders) rather than a separate simulated model, so what this shows
is guaranteed consistent with what an operator would see if they went and
looked at the topology themselves:

  1. Parse the proposed config for anything that would sever a *confirmed*
     topology link on this device -- an interface `shutdown`, or an
     inbound ACL applied to an interface that would deny all traffic
     across it (same "would this lock out/black-hole" logic
     validation_engine's `_check_mgmt_lockout` uses, generalized here to
     any interface, not just the management one).
  2. Remove exactly those edges from a copy of the live graph.
  3. BFS from this device in both the original graph and the edge-removed
     graph, hop-count every other device reaches it in each. A device
     that was reachable before and isn't after has genuinely lost
     connectivity (no redundant path exists in the graph this app knows
     about) -- flagged as "isolated". A device whose hop count increased
     but didn't disappear failed over to a longer path -- flagged as
     "degraded" (still up, but likely to see a brief blip during
     reconvergence). Everything else is unaffected.

This only ever flags edges topology discovery has actually confirmed
(LLDP/CDP/GNS3/subnet-inference -- see topology_service's module
docstring for the trust ordering) -- same "degrade gracefully, never
invent inventory we don't have" posture the rest of the pre-deployment
checks use. An interface that's a real uplink but hasn't been discovered
yet simply won't show up as a removed link.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.device import Device
from app.services import topology_service

_INTERFACE_RE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE)
_BLOCK_ENDERS = {"interface", "router", "line"}
_ACL_APPLY_RE = re.compile(r"(?:ip\s+)?access-group\s+(\S+)\s+in\b", re.IGNORECASE)
_ACL_DEF_NAMED_RE = re.compile(r"ip\s+access-list\s+(?:standard|extended)\s+(\S+)", re.IGNORECASE)
_ACL_DEF_NUMBERED_RE = re.compile(r"^access-list\s+(\d+)\b", re.IGNORECASE)
_ACL_PERMIT_RE = re.compile(r"^permit\b", re.IGNORECASE)


@dataclass
class RemovedLink:
    interface: str
    reason: str
    neighbor_device_id: str | None
    neighbor_hostname: str | None
    neighbor_port: str | None


@dataclass
class DeviceImpact:
    device_id: str
    hostname: str
    device_role: str | None
    before_hop_count: int
    after_hop_count: int | None  # None = fully isolated
    status: str  # "isolated" | "degraded"


@dataclass
class ImpactSimulationResult:
    device_id: str
    hostname: str
    affected_interfaces: list[str] = field(default_factory=list)
    removed_links: list[RemovedLink] = field(default_factory=list)
    isolated_devices: list[DeviceImpact] = field(default_factory=list)
    degraded_devices: list[DeviceImpact] = field(default_factory=list)
    reachable_unaffected_count: int = 0
    total_dependent_count: int = 0
    classification: str = "safe"  # "safe" | "caution" | "danger"
    summary: str = ""


def _interface_blocks(config_text: str) -> dict[str, list[str]]:
    """Same grouping approach as validation_engine._split_interface_blocks,
    kept as an independent copy rather than a shared import since the two
    modules' notion of "block end" could reasonably diverge later."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = _INTERFACE_RE.match(stripped)
        if m:
            current = m.group(1)
            blocks.setdefault(current, [])
            continue
        first_token = stripped.split()[0].lower() if stripped.split() else ""
        if stripped.lower() in ("exit", "!") or first_token in _BLOCK_ENDERS:
            current = None
            continue
        if current is not None:
            blocks[current].append(stripped)
    return blocks


def _acl_body_denies_everything(inventory_text: str, acl_name: str) -> bool:
    """True only if the ACL's body is actually visible in this change's
    inventory (proposed + current config) and contains zero `permit`
    lines -- an ACL we can't see the body of is never flagged (nothing to
    conflict-check against), matching validation_engine's "can't confirm
    it, don't fail it" posture for cross-checks."""
    lines: list[str] = []
    in_named_block = False
    for raw_line in inventory_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        named = _ACL_DEF_NAMED_RE.match(stripped)
        if named:
            in_named_block = named.group(1) == acl_name
            continue
        if in_named_block:
            first_token = stripped.split()[0].lower() if stripped.split() else ""
            if stripped.lower() in ("exit", "!") or (first_token in _BLOCK_ENDERS and not stripped.lower().startswith("access-list")):
                in_named_block = False
                continue
            lines.append(stripped)
            continue
        numbered = _ACL_DEF_NUMBERED_RE.match(stripped)
        if numbered and numbered.group(1) == acl_name:
            lines.append(stripped)
    if not lines:
        return False
    return not any(_ACL_PERMIT_RE.match(line) for line in lines)


def _find_affected_interfaces(proposed_config: str, current_config: str | None) -> dict[str, str]:
    """Interfaces whose proposed config would sever a live topology link:
    an explicit `shutdown`, or an inbound ACL that (as far as this
    change's visible inventory shows) permits nothing at all."""
    affected: dict[str, str] = {}
    inventory_text = (current_config or "") + "\n" + proposed_config
    blocks = _interface_blocks(proposed_config)

    for iface, lines in blocks.items():
        if any(line.lower() == "shutdown" for line in lines):
            affected[iface] = "interface would be administratively shut down"
            continue  # shutdown already fully severs the link; no need to also check its ACL
        for line in lines:
            m = _ACL_APPLY_RE.search(line)
            if m and _acl_body_denies_everything(inventory_text, m.group(1)):
                affected[iface] = f"inbound ACL '{m.group(1)}' would deny all traffic (no permit rule found)"
                break
    return affected


def _bfs_hop_counts(adjacency: dict[str, set[str]], start: str) -> dict[str, int]:
    dist: dict[str, int] = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor not in dist:
                dist[neighbor] = dist[current] + 1
                queue.append(neighbor)
    return dist


def simulate_impact(db: Session, device: Device, proposed_config: str, current_config: str | None = None) -> ImpactSimulationResult:
    """Runs the dry-run simulation for a proposed config against `device`.
    Safe to call before a change request even exists (New Change Request
    form, live as the operator types) or against an already-submitted
    change request's stored current_config/proposed_config."""
    device_id = str(device.id)
    affected = _find_affected_interfaces(proposed_config, current_config)

    graph = topology_service.build_topology(db)
    nodes_by_id = {n.id: n for n in graph.nodes}

    removed_links: list[RemovedLink] = []
    removed_edge_ids: set[int] = set()
    for edge in graph.edges:
        local_port = None
        if edge.source == device_id and edge.local_port in affected:
            local_port, neighbor_id, neighbor_port = edge.local_port, edge.target, edge.neighbor_port
        elif edge.target == device_id and edge.neighbor_port in affected:
            local_port, neighbor_id, neighbor_port = edge.neighbor_port, edge.source, edge.local_port
        else:
            continue
        removed_edge_ids.add(id(edge))
        neighbor = nodes_by_id.get(neighbor_id)
        removed_links.append(
            RemovedLink(
                interface=local_port,
                reason=affected[local_port],
                neighbor_device_id=neighbor_id,
                neighbor_hostname=neighbor.hostname if neighbor else None,
                neighbor_port=neighbor_port,
            )
        )

    def build_adjacency(*, skip_removed: bool) -> dict[str, set[str]]:
        adjacency: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
        for edge in graph.edges:
            if skip_removed and id(edge) in removed_edge_ids:
                continue
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)
        return adjacency

    before = _bfs_hop_counts(build_adjacency(skip_removed=False), device_id)
    after = _bfs_hop_counts(build_adjacency(skip_removed=True), device_id) if removed_edge_ids else before

    isolated: list[DeviceImpact] = []
    degraded: list[DeviceImpact] = []
    unaffected_count = 0
    for node_id, before_hops in before.items():
        if node_id == device_id:
            continue
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        after_hops = after.get(node_id)
        if after_hops is None:
            isolated.append(
                DeviceImpact(
                    device_id=node_id,
                    hostname=node.hostname,
                    device_role=node.device_role,
                    before_hop_count=before_hops,
                    after_hop_count=None,
                    status="isolated",
                )
            )
        elif after_hops > before_hops:
            degraded.append(
                DeviceImpact(
                    device_id=node_id,
                    hostname=node.hostname,
                    device_role=node.device_role,
                    before_hop_count=before_hops,
                    after_hop_count=after_hops,
                    status="degraded",
                )
            )
        else:
            unaffected_count += 1

    isolated.sort(key=lambda d: (d.device_role != "core", d.hostname))
    degraded.sort(key=lambda d: (d.device_role != "core", d.hostname))

    if isolated:
        classification = "danger"
    elif degraded or removed_links:
        classification = "caution"
    else:
        classification = "safe"

    if not affected:
        summary = "No interface shutdowns or traffic-denying ACLs detected in this change -- topology unaffected."
    elif isolated:
        core_isolated = sum(1 for d in isolated if d.device_role == "core")
        summary = (
            f"This change would isolate {len(isolated)} device{'s' if len(isolated) != 1 else ''} with no "
            "redundant topology path"
            + (f", including {core_isolated} core device{'s' if core_isolated != 1 else ''}" if core_isolated else "")
            + " -- do not deploy without a maintenance window and a redundant path in place first."
        )
    elif degraded:
        summary = (
            f"No device would be fully isolated, but {len(degraded)} device{'s' if len(degraded) != 1 else ''} "
            "would fail over to a longer alternate path -- expect a brief disruption during reconvergence."
        )
    elif removed_links:
        summary = (
            f"{len(removed_links)} link{'s' if len(removed_links) != 1 else ''} would go down, but every "
            "dependent device has a redundant path in the known topology -- no reachability loss detected."
        )
    else:
        summary = "No topology impact detected."

    return ImpactSimulationResult(
        device_id=device_id,
        hostname=device.hostname,
        affected_interfaces=sorted(affected.keys()),
        removed_links=removed_links,
        isolated_devices=isolated,
        degraded_devices=degraded,
        reachable_unaffected_count=unaffected_count,
        total_dependent_count=len(before) - 1,
        classification=classification,
        summary=summary,
    )
