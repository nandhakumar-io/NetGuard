"""AI Configuration Analyzer (SRS 6.2 / FR-6).

Detects risky network changes in a proposed config before it is deployed.
Two things are intentionally decoupled here:

  1. Network-aware analysis (VLAN conflicts, routing loops, ACL conflicts,
     OSPF adjacency impact, duplicate IPs, plus the original regex rules)
     lives in `_NetworkAwareChecks` and is shared by every scorer.
  2. The scorer itself is pluggable behind the `RiskScorer` interface so a
     future ML/LLM-backed scorer can be swapped in without touching any
     caller. Callers only ever call `analyze()`; nothing else in the
     codebase should import a concrete scorer class directly.

Selection is controlled by `settings.RISK_ENGINE_BACKEND` ("rules" | "llm").
"""
from __future__ import annotations

import ipaddress
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field

from app.core.config import settings
from app.schemas.change_request import RiskAnalysisResult
from app.services import config_format_service, diff_engine

# (regex pattern, weight, human-readable finding)
RISK_RULES: list[tuple[str, int, str]] = [
    (r"\bno\s+router\s+bgp\b", 35, "BGP process removal detected"),
    (r"\bno\s+neighbor\s+\S+", 30, "BGP neighbor removal detected"),
    (r"\bno\s+router\s+ospf\b", 30, "OSPF process removal detected"),
    (r"\bip\s+route\s+0\.0\.0\.0\s+0\.0\.0\.0\b", 15, "Default route change detected"),
    (r"\bno\s+ip\s+route\b", 15, "Static route removal detected"),
    (r"\baccess-list\s+\d+\s+deny\s+any\b", 15, "Broad ACL deny rule detected"),
    (r"\bno\s+vlan\s+\d+\b", 10, "VLAN removal detected"),
    (r"\bip\s+address\s+(\d{1,3}\.){3}\d{1,3}\s+255\.255\.255\.255\b", 10, "Suspicious /32 subnet mask"),
]

# Matched line-by-line rather than folded into RISK_RULES above, because a
# plain `\bshutdown\b` regex can't distinguish "shutdown" (administratively
# disabling an interface) from "no shutdown" (bringing one back up) -- both
# contain the same substring. Treated separately so an interface being
# *enabled* isn't misreported and scored as if it were being disabled.
_SHUTDOWN_LINE_RE = re.compile(r"^\s*shutdown\s*$", re.IGNORECASE | re.MULTILINE)
_NO_SHUTDOWN_LINE_RE = re.compile(r"^\s*no\s+shutdown\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Shared config parsing / network-aware checks
# ---------------------------------------------------------------------------

_IFACE_RE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_IP_ADDR_RE = re.compile(r"ip\s+address\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
_VLAN_DECL_RE = re.compile(r"^vlan\s+(\d+)(?:\s*\n\s*name\s+(\S+))?", re.IGNORECASE | re.MULTILINE)
_SWITCHPORT_ACCESS_VLAN_RE = re.compile(r"switchport\s+access\s+vlan\s+(\d+)", re.IGNORECASE)
_NO_VLAN_RE = re.compile(r"\bno\s+vlan\s+(\d+)\b", re.IGNORECASE)
_ROUTE_RE = re.compile(
    r"\bip\s+route\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\S+)", re.IGNORECASE
)
_ACL_RULE_RE = re.compile(
    r"access-list\s+(\d+)\s+(permit|deny)\s+(\S+)(?:\s+(\S+))?(?:\s+(\S+))?", re.IGNORECASE
)
_OSPF_NETWORK_RE = re.compile(
    r"network\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})\s+area\s+(\S+)", re.IGNORECASE
)
_OSPF_PASSIVE_RE = re.compile(r"passive-interface\s+(\S+)", re.IGNORECASE)
_OSPF_PROCESS_RE = re.compile(r"router\s+ospf\s+\d+", re.IGNORECASE)

# --- BGP / EVPN-VXLAN structured facts -----------------------------------
# Two dialect families are parsed rather than one:
#   - "curly"/indented CLI (IOS, IOS-XE, NX-OS, EOS): `router bgp <asn>` /
#     `neighbor <ip> remote-as <asn>`
#   - Junos `set` syntax: `set protocols bgp group <g> neighbor <ip>
#     peer-as <asn>`
# so BGP/EVPN risk isn't only detected on Cisco-family text the way the
# original keyword rules (RISK_RULES `no router bgp` / `no neighbor`) are --
# those still fire for IOS-style removals, but a Junos `delete protocols
# bgp group X neighbor Y` previously produced *no* structured finding at
# all, only whatever a bare keyword regex happened to catch.
_BGP_ASN_RE = re.compile(r"^\s*router\s+bgp\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_BGP_NEIGHBOR_REMOTE_AS_RE = re.compile(
    r"^\s*neighbor\s+(\S+)\s+remote-as\s+(\d+)", re.IGNORECASE | re.MULTILINE
)
_BGP_NEIGHBOR_RM_RE = re.compile(
    r"^\s*neighbor\s+(\S+)\s+route-map\s+(\S+)\s+(in|out)", re.IGNORECASE | re.MULTILINE
)
_BGP_NEIGHBOR_PL_RE = re.compile(
    r"^\s*neighbor\s+(\S+)\s+prefix-list\s+(\S+)\s+(in|out)", re.IGNORECASE | re.MULTILINE
)
_JUNOS_BGP_ASN_RE = re.compile(r"^set\s+routing-options\s+autonomous-system\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_JUNOS_BGP_NEIGHBOR_RE = re.compile(
    r"^(set|delete)\s+protocols\s+bgp\s+group\s+\S+\s+neighbor\s+(\S+)\s+peer-as\s+(\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_NO_BGP_NEIGHBOR_RE = re.compile(r"\bno\s+neighbor\s+(\S+)\b", re.IGNORECASE)

# EVPN/VXLAN overlay -- a change here silently breaks Type-2/Type-3 route
# exchange or the data-plane VTEP mesh without ever touching "underlay"
# constructs (interfaces, static routes) that the older checks look at.
_VNI_RE = re.compile(r"\b(?:vni|vxlan-id)\s+(\d+)\b", re.IGNORECASE)
_NVE_INTERFACE_RE = re.compile(r"^interface\s+nve\s*\d+", re.IGNORECASE | re.MULTILINE)
_EVPN_AF_RE = re.compile(r"\baddress-family\s+l2vpn\s+evpn\b", re.IGNORECASE)
_JUNOS_EVPN_RE = re.compile(r"^set\s+.*\bprotocols\s+evpn\b", re.IGNORECASE | re.MULTILINE)
_JUNOS_VNI_RE = re.compile(r"^set\s+.*\bvni\s+(\d+)\b", re.IGNORECASE | re.MULTILINE)
_NO_VNI_RE = re.compile(r"\bno\s+(?:vni|member\s+vni)\s+(\d+)\b", re.IGNORECASE)


@dataclass
class ParsedConfig:
    """Structured facts extracted from a raw device config, used by the
    network-aware checks below. Cheap regex-based parsing on purpose --
    this isn't a full CLI parser, just enough structure to catch the
    conflict/impact classes called out in SRS 6.2 / FR-6."""

    interfaces: dict[str, str] = field(default_factory=dict)  # name -> ip/prefix (for ip route iface refs)
    ip_addresses: list[tuple[str, str]] = field(default_factory=list)  # (ip, mask)
    vlans_declared: set[int] = field(default_factory=set)
    vlans_removed: set[int] = field(default_factory=set)
    vlans_in_use_by_ports: set[int] = field(default_factory=set)
    static_routes: list[tuple[str, str, str]] = field(default_factory=list)  # (network, mask, next_hop)
    acl_rules: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))  # acl# -> [(action, target)]
    ospf_networks: list[tuple[str, str, str]] = field(default_factory=list)
    ospf_passive_interfaces: set[str] = field(default_factory=set)
    has_ospf_process: bool = False

    # -- BGP --
    bgp_asn: str | None = None
    has_bgp_process: bool = False
    # neighbor_ip -> remote_as. Populated from both IOS-family
    # `neighbor X remote-as Y` and Junos `set ... neighbor X peer-as Y`,
    # so a caller comparing current vs proposed sees the same shape of
    # fact regardless of which dialect the device speaks.
    bgp_neighbors: dict[str, str] = field(default_factory=dict)
    bgp_neighbors_removed: set[str] = field(default_factory=set)  # explicit `no neighbor` / Junos `delete ... neighbor`
    # neighbor_ip -> set of (policy_kind, name, direction), policy_kind in
    # {"route-map", "prefix-list"} -- used to flag a neighbor losing its
    # inbound/outbound policy filter rather than just losing the session.
    bgp_neighbor_policies: dict[str, set[tuple[str, str, str]]] = field(default_factory=lambda: defaultdict(set))

    # -- EVPN / VXLAN overlay --
    has_evpn_process: bool = False
    nve_interfaces: set[str] = field(default_factory=set)
    vnis_declared: set[int] = field(default_factory=set)
    vnis_removed: set[int] = field(default_factory=set)


def parse_config(text: str) -> ParsedConfig:
    parsed = ParsedConfig()
    for m in _IP_ADDR_RE.finditer(text):
        parsed.ip_addresses.append((m.group(1), m.group(2)))
    for m in _VLAN_DECL_RE.finditer(text):
        parsed.vlans_declared.add(int(m.group(1)))
    for m in _NO_VLAN_RE.finditer(text):
        parsed.vlans_removed.add(int(m.group(1)))
    for m in _SWITCHPORT_ACCESS_VLAN_RE.finditer(text):
        parsed.vlans_in_use_by_ports.add(int(m.group(1)))
    for m in _ROUTE_RE.finditer(text):
        parsed.static_routes.append((m.group(1), m.group(2), m.group(3)))
    for m in _ACL_RULE_RE.finditer(text):
        acl_id, action, target = m.group(1), m.group(2), m.group(3)
        parsed.acl_rules[acl_id].append((action.lower(), target.lower()))
    for m in _OSPF_NETWORK_RE.finditer(text):
        parsed.ospf_networks.append((m.group(1), m.group(2), m.group(3)))
    for m in _OSPF_PASSIVE_RE.finditer(text):
        parsed.ospf_passive_interfaces.add(m.group(1).lower())
    parsed.has_ospf_process = bool(_OSPF_PROCESS_RE.search(text))

    # -- BGP (IOS-family + Junos set-style) --
    asn_match = _BGP_ASN_RE.search(text) or _JUNOS_BGP_ASN_RE.search(text)
    if asn_match:
        parsed.bgp_asn = asn_match.group(1)
        parsed.has_bgp_process = True
    for m in _BGP_NEIGHBOR_REMOTE_AS_RE.finditer(text):
        parsed.bgp_neighbors[m.group(1).lower()] = m.group(2)
        parsed.has_bgp_process = True
    for m in _JUNOS_BGP_NEIGHBOR_RE.finditer(text):
        action, neighbor, remote_as = m.group(1).lower(), m.group(2).lower(), m.group(3)
        parsed.has_bgp_process = True
        if action == "delete":
            parsed.bgp_neighbors_removed.add(neighbor)
        else:
            parsed.bgp_neighbors[neighbor] = remote_as
    for m in _NO_BGP_NEIGHBOR_RE.finditer(text):
        parsed.bgp_neighbors_removed.add(m.group(1).lower())
    for m in _BGP_NEIGHBOR_RM_RE.finditer(text):
        parsed.bgp_neighbor_policies[m.group(1).lower()].add(("route-map", m.group(2), m.group(3).lower()))
    for m in _BGP_NEIGHBOR_PL_RE.finditer(text):
        parsed.bgp_neighbor_policies[m.group(1).lower()].add(("prefix-list", m.group(2), m.group(3).lower()))

    # -- EVPN / VXLAN overlay (IOS/NX-OS/EOS-style + Junos set-style) --
    parsed.has_evpn_process = bool(_EVPN_AF_RE.search(text) or _JUNOS_EVPN_RE.search(text))
    if _NVE_INTERFACE_RE.search(text):
        parsed.nve_interfaces.add("nve")
    for m in _VNI_RE.finditer(text):
        parsed.vnis_declared.add(int(m.group(1)))
    for m in _JUNOS_VNI_RE.finditer(text):
        parsed.vnis_declared.add(int(m.group(1)))
    for m in _NO_VNI_RE.finditer(text):
        parsed.vnis_removed.add(int(m.group(1)))

    return parsed


def _added_lines_from_diff(diff_text: str) -> str:
    """Pull just the newly-introduced lines out of a unified diff (as
    produced by `diff_engine.generate_diff`), stripping the leading '+' and
    the '+++ proposed_configuration' file-header line. Used so the keyword
    RISK_RULES only ever see what's actually changing, not the whole
    proposed config -- a `no router bgp` that already existed before this
    change (and shows up as unchanged context, not a `+` line) shouldn't be
    re-flagged as a newly proposed risk.
    """
    added = []
    for line in diff_text.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


def _wildcard_to_prefixlen(mask: str) -> int | None:
    """OSPF `network` lines use a wildcard mask (inverted netmask). Convert
    to a prefix length so networks can be compared for overlap."""
    try:
        octets = [int(o) for o in mask.split(".")]
        inverted = ".".join(str(255 - o) for o in octets)
        return ipaddress.IPv4Network(f"0.0.0.0/{inverted}", strict=False).prefixlen
    except (ValueError, ipaddress.AddressValueError):
        return None


def _networks_overlap(net_a: tuple[str, str], net_b: tuple[str, str]) -> bool:
    try:
        a = ipaddress.IPv4Network(f"{net_a[0]}/{net_a[1]}", strict=False)
        b = ipaddress.IPv4Network(f"{net_b[0]}/{net_b[1]}", strict=False)
        return a.overlaps(b)
    except (ValueError, ipaddress.AddressValueError):
        return False


class NetworkAwareChecks:
    """Config-conflict / blast-radius checks that go beyond simple keyword
    matching. Shared by every scorer implementation so a future ML/LLM
    scorer can call the same primitives instead of re-deriving them from
    raw text.

    `other_device_configs` is an optional {hostname: config_text} map of the
    *other* devices in the fleet (their latest known-good config), enabling
    cross-device duplicate-IP / VLAN-conflict detection instead of only
    looking within a single device's before/after.
    """

    def __init__(
        self,
        proposed_text: str,
        current_text: str | None,
        other_device_configs: dict[str, str] | None = None,
    ) -> None:
        self._proposed_text = proposed_text
        self.proposed = parse_config(proposed_text)
        self.current = parse_config(current_text) if current_text else None
        self.other_device_configs = other_device_configs or {}

    def run(self) -> list[tuple[str, int]]:
        findings: list[tuple[str, int]] = []
        findings += self._duplicate_ip_within_device()
        findings += self._duplicate_ip_across_fleet()
        findings += self._vlan_conflicts()
        findings += self._routing_loop_risk()
        findings += self._acl_conflicts()
        findings += self._ospf_adjacency_impact()
        findings += self._bgp_impact()
        findings += self._evpn_vxlan_impact()
        return findings

    # -- BGP impact ------------------------------------------------------
    # Weighted above VLAN/interface/ACL findings on purpose: a BGP session
    # or policy change is fleet-wide (every downstream prefix learned via
    # that peer) rather than local to one device/port, and generic
    # diff-based keyword scoring can't tell "neighbor policy swapped" from
    # "neighbor description edited" -- both are just lines that changed.

    def _bgp_impact(self) -> list[tuple[str, int]]:
        findings: list[tuple[str, int]] = []
        if not self.current or not (self.current.has_bgp_process or self.proposed.has_bgp_process):
            return findings

        if self.current.bgp_asn and self.proposed.bgp_asn and self.current.bgp_asn != self.proposed.bgp_asn:
            findings.append(
                (f"BGP local AS changed ({self.current.bgp_asn} -> {self.proposed.bgp_asn}) -- every eBGP session on this device will reset", 35)
            )

        removed = (set(self.current.bgp_neighbors) - set(self.proposed.bgp_neighbors)) | self.proposed.bgp_neighbors_removed
        for neighbor in sorted(removed):
            remote_as = self.current.bgp_neighbors.get(neighbor, "unknown")
            findings.append((f"BGP neighbor {neighbor} (AS {remote_as}) removed -- prefixes learned/advertised via this peer will be lost", 30))

        for neighbor, remote_as in self.proposed.bgp_neighbors.items():
            prior_as = self.current.bgp_neighbors.get(neighbor)
            if prior_as and prior_as != remote_as:
                findings.append((f"BGP neighbor {neighbor} remote-as changed ({prior_as} -> {remote_as}) -- session will flap and may form with an unintended peer", 25))

        # Policy attached to a *surviving* neighbor going away (route-map /
        # prefix-list removed while the session itself stays up) is the
        # dangerous, easy-to-miss case: the peer stays adjacent but starts
        # sending/receiving prefixes with no filter at all.
        for neighbor, current_policies in self.current.bgp_neighbor_policies.items():
            if neighbor in removed:
                continue
            proposed_policies = self.proposed.bgp_neighbor_policies.get(neighbor, set())
            dropped = current_policies - proposed_policies
            for kind, name, direction in dropped:
                findings.append((f"BGP neighbor {neighbor} lost its {direction}bound {kind} '{name}' -- route filtering on this peer is now weaker or absent", 20))

        return findings

    # -- EVPN / VXLAN overlay impact --------------------------------------

    def _evpn_vxlan_impact(self) -> list[tuple[str, int]]:
        findings: list[tuple[str, int]] = []
        if not self.current:
            return findings

        if self.current.has_evpn_process and not self.proposed.has_evpn_process:
            findings.append(("EVPN address-family/protocol removed -- Type-2/Type-3 route exchange for all overlay VNIs on this device stops", 35))

        removed_vnis = (self.current.vnis_declared - self.proposed.vnis_declared) | self.proposed.vnis_removed
        for vni in sorted(removed_vnis):
            findings.append((f"VXLAN VNI {vni} removed -- Layer-2 overlay reachability for this segment is lost fleet-wide, not just on this device", 25))

        if self.current.nve_interfaces and not self.proposed.nve_interfaces:
            findings.append(("NVE (VTEP) interface removed -- this device drops out of the VXLAN overlay entirely", 30))

        return findings

    # -- Duplicate IPs -----------------------------------------------------

    def _duplicate_ip_within_device(self) -> list[tuple[str, int]]:
        ips = [ip for ip, _mask in self.proposed.ip_addresses]
        if len(ips) != len(set(ips)):
            dupes = {ip for ip in ips if ips.count(ip) > 1}
            return [(f"Duplicate IP address within proposed configuration ({', '.join(sorted(dupes))})", 20)]
        return []

    def _duplicate_ip_across_fleet(self) -> list[tuple[str, int]]:
        findings = []
        proposed_ips = {ip for ip, _mask in self.proposed.ip_addresses}
        for hostname, other_text in self.other_device_configs.items():
            other_ips = {ip for ip, _mask in parse_config(other_text).ip_addresses}
            collisions = proposed_ips & other_ips
            if collisions:
                findings.append(
                    (
                        f"IP address conflict with device '{hostname}': {', '.join(sorted(collisions))} "
                        "already assigned elsewhere",
                        25,
                    )
                )
        return findings

    # -- VLAN conflicts ------------------------------------------------

    def _vlan_conflicts(self) -> list[tuple[str, int]]:
        findings = []
        # Removing a VLAN that a switchport is still actively assigned to
        stranded = self.proposed.vlans_removed & (
            self.proposed.vlans_in_use_by_ports | (self.current.vlans_in_use_by_ports if self.current else set())
        )
        if stranded:
            findings.append(
                (f"VLAN removal conflicts with active port assignment(s): VLAN {', '.join(map(str, sorted(stranded)))}", 25)
            )
        # Cross-device VLAN ID reused with a different name -- common
        # misconfiguration that breaks trunking/segmentation assumptions
        # (e.g. VLAN 20 = "GUEST" on one switch but "FINANCE" on another).
        proposed_names = {
            int(m.group(1)): (m.group(2) or "").lower() for m in _VLAN_DECL_RE.finditer(self._proposed_text)
        }
        for hostname, other_text in self.other_device_configs.items():
            for match in _VLAN_DECL_RE.finditer(other_text):
                vlan_id, other_name = int(match.group(1)), (match.group(2) or "").lower()
                mine = proposed_names.get(vlan_id)
                if mine and other_name and mine != other_name:
                    findings.append(
                        (
                            f"VLAN {vlan_id} naming conflict: proposed name '{mine}' differs from "
                            f"'{other_name}' already configured on device '{hostname}'",
                            10,
                        )
                    )
        return findings

    # -- Routing loop risk ---------------------------------------------

    def _routing_loop_risk(self) -> list[tuple[str, int]]:
        findings = []
        routes = self.proposed.static_routes
        by_dest: dict[tuple[str, str], list[str]] = defaultdict(list)
        for network, mask, next_hop in routes:
            by_dest[(network, mask)].append(next_hop)
        # Same destination pointing back through more than one next-hop that
        # is itself one of the local interface/route next-hops is a classic
        # loop precursor (A -> B -> A). Flag when a next-hop for one route
        # equals the destination network of another route in the same batch.
        dests = {net for net, _mask in by_dest}
        for (network, mask), next_hops in by_dest.items():
            for nh in next_hops:
                if nh in dests and nh != network:
                    findings.append(
                        (f"Potential routing loop: static route to {network}/{mask} via {nh}, which is itself a routed destination", 20)
                    )
        return findings

    # -- ACL conflicts ---------------------------------------------------

    def _acl_conflicts(self) -> list[tuple[str, int]]:
        findings = []
        for acl_id, rules in self.proposed.acl_rules.items():
            permits = {target for action, target in rules if action == "permit"}
            denies = {target for action, target in rules if action == "deny"}
            conflicting = permits & denies
            if conflicting:
                findings.append(
                    (f"ACL {acl_id} has conflicting permit/deny rules for: {', '.join(sorted(conflicting))}", 15)
                )
            if "any" in denies and any(t != "any" for t in permits):
                # a trailing/blanket deny any that shadows earlier permits is
                # normal Cisco behavior, but an *inserted* deny any ahead of
                # existing permits (order lost in our simple parse) is the
                # dangerous case operators mean to catch here.
                findings.append((f"ACL {acl_id}: blanket 'deny any' may shadow other permit rules -- verify rule order", 10))
        return findings

    # -- OSPF adjacency impact -------------------------------------------

    def _ospf_adjacency_impact(self) -> list[tuple[str, int]]:
        findings = []
        if not self.current:
            return findings
        if self.current.has_ospf_process and not self.proposed.has_ospf_process:
            return findings  # already caught by the "OSPF process removal" regex rule
        removed_networks = set(self.current.ospf_networks) - set(self.proposed.ospf_networks)
        for network, mask, area in removed_networks:
            findings.append(
                (f"OSPF network {network}/{_wildcard_to_prefixlen(mask)} removed from area {area} -- adjacent neighbors may drop", 20)
            )
        newly_passive = self.proposed.ospf_passive_interfaces - self.current.ospf_passive_interfaces
        for iface in newly_passive:
            findings.append((f"Interface {iface} newly marked OSPF passive -- existing adjacencies on it will drop", 15))
        return findings


# ---------------------------------------------------------------------------
# Pluggable scorer interface
# ---------------------------------------------------------------------------

class RiskScorer(ABC):
    """Interface every risk-scoring backend implements. `analyze()` at the
    bottom of this module is the only thing callers should use; it picks a
    concrete scorer based on `settings.RISK_ENGINE_BACKEND`."""

    @abstractmethod
    def score(
        self,
        proposed_config: str,
        current_config: str | None = None,
        other_device_configs: dict[str, str] | None = None,
        llm_timeout: float | None = None,
    ) -> RiskAnalysisResult:
        raise NotImplementedError


class RuleBasedScorer(RiskScorer):
    """v1 scorer: weighted regex rules + the network-aware checks above.
    Deterministic and dependency-free -- the default backend.

    The regex rules are diff-aware: when a current_config is available they
    run only against the lines diff_engine.generate_diff marks as added,
    not the whole proposed config (see `_added_lines_from_diff`). The
    NetworkAwareChecks below are already before/after-aware at the
    structured level (stranded VLANs, OSPF adjacency impact, etc. all
    compare `self.current` vs `self.proposed`), so they're unaffected."""

    def score(
        self,
        proposed_config: str,
        current_config: str | None = None,
        other_device_configs: dict[str, str] | None = None,
        llm_timeout: float | None = None,
    ) -> RiskAnalysisResult:
        findings: list[str] = []
        score = 0

        # Keyword rules run against only the *changed* lines when we have a
        # current_config to diff against -- scanning the full proposed text
        # would re-flag risky-looking lines that were already present and
        # untouched (e.g. an existing `shutdown` on an unrelated interface),
        # producing findings that don't map to what this change actually
        # does. Falls back to the full text when there's no current_config
        # to diff against (e.g. first-ever config for a device).
        if current_config is not None:
            scan_text = _added_lines_from_diff(diff_engine.generate_diff(current_config, proposed_config))
        else:
            scan_text = proposed_config
        scan_text = scan_text.lower()

        for pattern, weight, message in RISK_RULES:
            if re.search(pattern, scan_text):
                findings.append(message)
                score += weight

        shutdown_count = len(_SHUTDOWN_LINE_RE.findall(scan_text))
        no_shutdown_count = len(_NO_SHUTDOWN_LINE_RE.findall(scan_text))
        # A bare "shutdown" line also matches "no shutdown" as a substring
        # search but not as ^\s*shutdown\s*$ line match, so these two
        # counts are already mutually exclusive -- no double counting.
        if shutdown_count:
            findings.append(
                f"Interface shutdown detected ({shutdown_count} interface{'s' if shutdown_count != 1 else ''} being administratively disabled)"
            )
            score += 20
        if no_shutdown_count:
            findings.append(
                f"Interface(s) being administratively enabled ({no_shutdown_count} interface{'s' if no_shutdown_count != 1 else ''} — 'no shutdown')"
            )
            # Bringing an interface up isn't inherently risky the way taking
            # one down is -- no score contribution, just visibility in the
            # findings list so the reviewer sees it called out.

        checks = NetworkAwareChecks(proposed_config, current_config, other_device_configs)
        for message, weight in checks.run():
            findings.append(message)
            score += weight

        score = min(score, 100)
        classification, recommendation = _classify(score)

        if not findings:
            findings.append("No significant risk patterns detected")

        return RiskAnalysisResult(
            risk_score=score,
            classification=classification,
            recommendation=recommendation,
            findings=findings,
        )


class LLMScorer(RiskScorer):
    """Optional model-backed scorer, selected via
    `settings.RISK_ENGINE_BACKEND = "llm"`. Runs the same deterministic
    NetworkAwareChecks first (so hard conflicts are never missed even if
    the model call fails or is unavailable), then asks the configured
    provider (`settings.RISK_ENGINE_LLM_PROVIDER`: "anthropic" or
    "ollama") to reason about softer/contextual risk and merges its
    findings in. Falls back to the rule-based score alone if the provider
    has no credential/isn't reachable or the call errors -- enabling this
    backend can never make analysis unavailable -- but unlike the earlier
    version, that fallback is no longer silent: the result's
    `llm_applied`/`llm_error` fields tell the caller whether the model
    pass actually happened, so a CR can record it (see
    app.api.change_requests) instead of a reviewer having no way to tell
    a rule-only score from an LLM-reviewed one.
    """

    def __init__(self) -> None:
        self._rule_scorer = RuleBasedScorer()

    def score(
        self,
        proposed_config: str,
        current_config: str | None = None,
        other_device_configs: dict[str, str] | None = None,
        llm_timeout: float | None = None,
    ) -> RiskAnalysisResult:
        # Compute the deterministic rule-based score first and hold onto it
        # as a guaranteed-safe fallback -- everything below this line
        # (provider dispatch, the network call itself, and assembling the
        # merged result from whatever the model handed back) is wrapped in
        # one broad try/except so a bad/unexpected LLM response can never
        # take the whole change-request submission down with it. This used
        # to only wrap the _call_llm() call itself; a failure in result
        # assembly (e.g. the model returning a non-numeric
        # additional_risk_points, or a findings value that isn't actually a
        # list) still propagated as a raw 500 with nothing in the response
        # body to diagnose it from.
        base = self._rule_scorer.score(proposed_config, current_config, other_device_configs)
        provider = settings.RISK_ENGINE_LLM_PROVIDER

        if provider == "anthropic" and not settings.ANTHROPIC_API_KEY:
            base.llm_error = "RISK_ENGINE_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not configured"
            return base
        if provider == "ollama" and not settings.OLLAMA_BASE_URL:
            base.llm_error = "RISK_ENGINE_LLM_PROVIDER=ollama but OLLAMA_BASE_URL is not configured"
            return base
        if provider not in ("anthropic", "ollama"):
            base.llm_error = f"Unknown RISK_ENGINE_LLM_PROVIDER '{provider}'"
            return base

        try:
            extra_findings, extra_score = self._call_llm(provider, proposed_config, current_config, llm_timeout)
            extra_findings = [str(f) for f in extra_findings] if isinstance(extra_findings, (list, tuple)) else []
            extra_score = int(extra_score)

            findings = base.findings + extra_findings
            score = min(base.risk_score + extra_score, 100)
            classification, recommendation = _classify(score)
            return RiskAnalysisResult(
                risk_score=score, classification=classification, recommendation=recommendation,
                findings=findings, llm_applied=True, llm_error=None,
            )
        except Exception as exc:
            # Never let a downstream model outage -- or a malformed/
            # unexpected response shape from it -- block change analysis.
            # Degrade to the deterministic rule-based result, but say why,
            # so this is diagnosable from the API response instead of only
            # from a server-side traceback.
            base.llm_error = f"{provider} call failed: {exc}"
            return base

    def _call_llm(
        self, provider: str, proposed_config: str, current_config: str | None, timeout: float | None = None,
    ) -> tuple[list[str], int]:
        data = self._call_llm_raw(provider, self._prompt(proposed_config, current_config), timeout)
        return list(data.get("findings", [])), int(data.get("additional_risk_points", 0))

    def _call_llm_raw(self, provider: str, prompt: str, timeout: float | None = None) -> dict:
        """Shared provider dispatch: sends `prompt`, returns the parsed JSON
        response body. Used by both the change-request risk prompt
        (_prompt/_call_llm above) and the drift-analysis prompt
        (_drift_prompt/analyze_drift below) so there's one place that knows
        how to actually talk to Anthropic/Ollama and parse a JSON reply out
        of it, instead of two near-duplicate call paths drifting apart.
        """
        if provider == "ollama":
            return self._call_ollama_raw(prompt, timeout)
        return self._call_anthropic_raw(prompt, timeout)

    @staticmethod
    def _prompt(proposed_config: str, current_config: str | None) -> str:
        # Same defensive cap _drift_prompt applies to the unified diff --
        # this prompt previously sent the *entire* current_config and
        # proposed_config untruncated, so a full device config (which,
        # before the rpc-reply envelope was stripped in
        # config_format_service, could run to thousands of XML lines)
        # produced a multi-thousand-token prompt on every single change
        # request submission, not just large-scale drift scans. That's
        # slow to process on a local model regardless of OLLAMA_TIMEOUT_
        # SECONDS and risks blowing past OLLAMA_NUM_CTX, so trim each
        # side independently the same way.
        current = current_config or "(none provided)"
        proposed = proposed_config
        truncated = False
        if len(current) > _MAX_DIFF_CHARS_FOR_PROMPT:
            current = current[:_MAX_DIFF_CHARS_FOR_PROMPT]
            truncated = True
        if len(proposed) > _MAX_DIFF_CHARS_FOR_PROMPT:
            proposed = proposed[:_MAX_DIFF_CHARS_FOR_PROMPT]
            truncated = True
        return (
            "You are a network change-risk reviewer. Given the proposed device "
            "config (and current config, if provided), identify additional risks "
            "not already obvious from simple keyword matching. Respond ONLY with "
            'JSON: {"findings": ["..."], "additional_risk_points": <0-30 int>}.\n\n'
            f"CURRENT CONFIG:\n{current}\n\n"
            f"PROPOSED CONFIG:\n{proposed}"
            + ("\n\n[config truncated for length]" if truncated else "")
        )

    def _call_anthropic_raw(self, prompt: str, timeout: float | None = None) -> dict:
        import json

        import anthropic

        client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=timeout if timeout is not None else settings.ANTHROPIC_TIMEOUT_SECONDS,
        )
        message = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1024,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(block.text for block in message.content if block.type == "text")
        return json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())

    def _call_ollama_raw(self, prompt: str, timeout: float | None = None) -> dict:
        """Calls a locally-running Ollama server's chat API
        (https://github.com/ollama/ollama/blob/main/docs/api.md) over plain
        HTTP -- no extra dependency, reuses httpx (already used by
        app.services.restconf_service). `format: "json"` asks Ollama to
        constrain the model's output to valid JSON so this doesn't need the
        same markdown-fence stripping the Anthropic path does; `stream:
        False` collects the full response in one call instead of NDJSON
        chunks. Requires the model to already be pulled on that server
        (`ollama pull <OLLAMA_MODEL>`) -- an unpulled model 404s the same as
        any other call failure and is caught by the caller's try/except.

        `num_ctx` is set explicitly on every call: Ollama defaults to a
        2048-token context window regardless of what the model itself
        supports, and silently truncates the prompt to fit rather than
        erroring -- so without this, a full device config was routinely
        cut off before the model ever reached the actual instructions.
        See settings.OLLAMA_NUM_CTX.
        """
        import json

        import httpx

        response = httpx.post(
            f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat",
            json={
                "model": settings.OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0, "num_ctx": settings.OLLAMA_NUM_CTX},
            },
            timeout=timeout if timeout is not None else settings.OLLAMA_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            # Ollama's own error body for this case is the actually useful
            # part (usually {"error": "model 'X' not found, try pulling it
            # first"}) -- a bare "Client error '404 Not Found' for url
            # ...api/chat" (what response.raise_for_status() below would
            # raise) reads like the *endpoint* is wrong, when in every
            # real case seen so far it's actually OLLAMA_MODEL naming a
            # tag that was never `ollama pull`ed on that server. Surface
            # Ollama's message so that's obvious without having to go
            # curl the API by hand to find out.
            try:
                detail = response.json().get("error", response.text)
            except Exception:  # noqa: BLE001
                detail = response.text
            raise RuntimeError(
                f"Ollama returned 404 for model '{settings.OLLAMA_MODEL}' at {settings.OLLAMA_BASE_URL}: "
                f"{detail}. Run `ollama list` on that server to see exactly which tags are pulled -- "
                f"OLLAMA_MODEL must match one of them exactly (e.g. 'llama3.1:8b', not 'llama3.1')."
            )
        response.raise_for_status()
        raw = response.json()["message"]["content"]
        return json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())


def _classify(score: int) -> tuple[str, str]:
    if score <= settings.RISK_LOW_MAX:
        return "Low Risk", "Safe to Deploy"
    elif score <= settings.RISK_MEDIUM_MAX:
        return "Medium Risk", "Review Recommended Before Deploy"
    return "Critical Risk", "Deployment Not Recommended -- Dual Approval Required"


# ---------------------------------------------------------------------------
# Drift-specific analysis (AI summary + risk score + clear-English diff)
# ---------------------------------------------------------------------------
# Unlike change-request scoring (gated behind settings.RISK_ENGINE_BACKEND,
# since a proposed change is scored before every single deploy and an
# operator may deliberately want the cheap rules-only path there), the
# Drift page's whole point is a human-readable AI summary of *why* a
# device's live config diverged from baseline and what to do about it --
# a rule engine can only ever hand back a list of regex-matched keywords
# ("BGP process removal detected"), not a written explanation. So
# analyze_drift() always attempts the configured LLM provider regardless of
# RISK_ENGINE_BACKEND, and only degrades to the deterministic rule-based
# score + a plain diff-line summary if no provider is configured or the
# call fails -- same tolerant "never make analysis unavailable" pattern as
# LLMScorer.score(), just always-on for this one caller.


@dataclass
class DriftAnalysisResult:
    risk_score: int
    ai_summary: str
    findings: list[str]
    llm_applied: bool
    llm_error: str | None = None
    cli_diff: list[str] = field(default_factory=list)


def _fallback_drift_summary(findings: list[str], added: int, removed: int) -> str:
    if not findings or findings == ["No significant risk patterns detected"]:
        if added == 0 and removed == 0:
            return "No drift detected. Live configuration matches baseline."
        return f"{added} line(s) added, {removed} line(s) removed. No high-risk patterns detected."
    return "; ".join(findings)


# Matches a single leaf XML element with text content on one line, e.g.
# "<name>3</name>" or '<address>172.17.1.26</address>' -- after
# netconf_service._pretty_xml, every element is on its own line, so most
# diff lines are exactly this shape.
_XML_LEAF_RE = re.compile(r"^<([\w.-]+)(?:\s[^>]*)?>([^<]+)</\1>\s*$")
# Matches a self-closing element with no text, e.g. "<shutdown/>" --
# presence/absence of these is usually the meaningful signal (a flag
# being set or cleared), not the tag name itself.
_XML_EMPTY_RE = re.compile(r"^<([\w.-]+)(?:\s[^>]*)?/>\s*$")
# Matches a bare opening/closing structural tag with nothing else on the
# line, e.g. "<GigabitEthernet>" or "</native>" -- these are just
# nesting/container noise once each element has its own line, not a
# change worth surfacing on its own.
_XML_CONTAINER_RE = re.compile(r"^</?[\w.-]+(?:\s[^>]*)?>\s*$")
# Matches the XML declaration / a processing instruction, e.g.
# '<?xml version="1.0" encoding="UTF-8"?>' -- these start with "<?", not
# "<" + a tag name, so none of the three patterns above ever matched them
# and they fell through to the raw-line return, which is exactly the
# "Removed: <?xml version=...?>" noise callers of _humanize_xml_line are
# trying to avoid.
_XML_DECL_RE = re.compile(r"^<\?[\w.-]+(?:\s[^>]*)?\?>\s*$")


def _humanize_xml_line(line: str) -> str | None:
    """Best-effort plain-English rendering of one pretty-printed XML diff
    line. Returns None for pure container/nesting lines (e.g. `<native>`)
    or the XML declaration, which carry no information on their own --
    callers skip those so the bullet list only shows lines that actually
    say something."""
    leaf = _XML_LEAF_RE.match(line)
    if leaf:
        tag, value = leaf.group(1), leaf.group(2).strip()
        return f"{tag.replace('-', ' ')}: {value}"
    empty = _XML_EMPTY_RE.match(line)
    if empty:
        return f"'{empty.group(1).replace('-', ' ')}' flag"
    if _XML_CONTAINER_RE.match(line) or _XML_DECL_RE.match(line):
        return None
    return line


def _fallback_clear_diff(diff_text: str, max_bullets: int = 25) -> list[str]:
    """Turns a raw unified diff into a plain-English bullet list when no
    LLM is available to write one -- "Added: <line>" / "Removed: <line>"
    per changed line, skipping the +++ / --- / @@ diff-header noise, so the
    Drift page always has *something* readable even with the LLM off.
    Each line is humanized via `_humanize_xml_line` (turning e.g.
    "<shutdown/>" into "'shutdown' flag" rather than showing the raw tag),
    and capped at `max_bullets` so a large-scale drift doesn't dump
    hundreds of one-line bullets onto the page."""
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") and line[1:].strip():
            content = _humanize_xml_line(line[1:].strip())
            if content is not None:
                lines.append(f"Added: {content}")
        elif line.startswith("-") and line[1:].strip():
            content = _humanize_xml_line(line[1:].strip())
            if content is not None:
                lines.append(f"Removed: {content}")
        if len(lines) >= max_bullets:
            lines.append(f"...and more changes not shown ({len(diff_text.splitlines())} diff lines total)")
            break
    return lines


# Cap on how much of the unified diff goes into the LLM prompt. Sending the
# *full* baseline + live config on top of the diff (the previous prompt did
# all three) meant a single drift scan could push several thousand tokens
# of largely-redundant XML into the model -- slower, more likely to hit
# num_ctx/timeout limits, and it left the model free to just quote chunks
# of raw config back as the "summary" instead of describing the change,
# which is exactly the unreadable wall-of-XML the Drift page was showing.
# The unified diff alone (already just the changed lines, plus a few lines
# of surrounding context from difflib) has everything needed to describe
# *what changed*; a huge diff still gets truncated defensively here since
# a from-scratch baseline (first-ever scan) can in principle diff the
# entire config as "added".
_MAX_DIFF_CHARS_FOR_PROMPT = 12_000


def _drift_prompt(diff_text: str) -> str:
    diff_for_prompt = diff_text
    truncated = False
    if len(diff_for_prompt) > _MAX_DIFF_CHARS_FOR_PROMPT:
        diff_for_prompt = diff_for_prompt[:_MAX_DIFF_CHARS_FOR_PROMPT]
        truncated = True

    return (
        "You are a network configuration drift analyst. Below is a unified "
        "diff between a device's approved baseline configuration and its "
        "current live configuration. Respond ONLY with JSON of the form: "
        '{"ai_summary": "<2-4 sentence plain-English paragraph summarizing '
        'what changed and why it matters operationally or from a security '
        'standpoint -- describe the changes, do not quote or reproduce raw '
        'config/XML text>", "clear_diff": ["<one short plain-English '
        "sentence per distinct change, e.g. 'Interface GigabitEthernet0/1 "
        "was administratively shut down' or 'ACL 101 gained a broad "
        'deny-any rule\'>"], "risk_score": <0-100 int, how risky this '
        'specific drift is>, "findings": ["<short risk finding phrase>", '
        '...]}. Every field must be written in your own words -- never '
        "copy XML tags, element names, or raw config lines into "
        "ai_summary or clear_diff; translate them into what changed on "
        "the device instead (e.g. a <shutdown/> element appearing under "
        "an interface means that interface was administratively shut "
        "down).\n\n"
        f"UNIFIED DIFF (- baseline / + live):\n{diff_for_prompt}"
        + ("\n\n[diff truncated for length]" if truncated else "")
    )


def analyze_drift(
    live_config: str,
    baseline_config: str,
    diff_text: str,
    added: int,
    removed: int,
) -> DriftAnalysisResult:
    rule_result = RuleBasedScorer().score(live_config, baseline_config)
    fallback_summary = _fallback_drift_summary(rule_result.findings, added, removed)

    # Path-based diff (baseline -> live) when both sides are XML -- far
    # more reliable than the line diff for both the human-readable
    # fallback bullets (no more raw XML leaking into ai_summary when the
    # LLM is unavailable) and the CLI-equivalent translation. None when
    # either side isn't XML (e.g. an SSH/NAPALM-sourced plain-CLI
    # config), in which case both stay empty/line-diff-based as before.
    structural_changes = config_format_service.xml_structural_diff(baseline_config, live_config)
    cli_diff = config_format_service.to_cli_commands(structural_changes) if structural_changes else []

    def _fallback(error: str | None) -> DriftAnalysisResult:
        summary = fallback_summary
        diff_bullets = (
            config_format_service.humanize_structural_diff(structural_changes)
            if structural_changes is not None
            else _fallback_clear_diff(diff_text)
        )
        if diff_bullets:
            summary = summary + "\n\nChanges:\n" + "\n".join(f"- {b}" for b in diff_bullets)
        return DriftAnalysisResult(
            risk_score=rule_result.risk_score, ai_summary=summary,
            findings=rule_result.findings, llm_applied=False, llm_error=error,
            cli_diff=cli_diff,
        )

    provider = settings.RISK_ENGINE_LLM_PROVIDER
    if provider == "anthropic" and not settings.ANTHROPIC_API_KEY:
        return _fallback("RISK_ENGINE_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not configured")
    if provider == "ollama" and not settings.OLLAMA_BASE_URL:
        return _fallback("RISK_ENGINE_LLM_PROVIDER=ollama but OLLAMA_BASE_URL is not configured")
    if provider not in ("anthropic", "ollama"):
        return _fallback(f"Unknown RISK_ENGINE_LLM_PROVIDER '{provider}'")

    try:
        scorer = LLMScorer()
        data = scorer._call_llm_raw(provider, _drift_prompt(diff_text))
    except Exception as exc:  # noqa: BLE001 - never let a model outage block drift detection
        return _fallback(f"{provider} call failed: {exc}")

    ai_summary = str(data.get("ai_summary") or "").strip() or fallback_summary
    clear_diff = [str(c) for c in (data.get("clear_diff") or []) if str(c).strip()]
    if clear_diff:
        ai_summary = ai_summary + "\n\nChanges:\n" + "\n".join(f"- {c}" for c in clear_diff)
    findings = [str(f) for f in (data.get("findings") or [])] or rule_result.findings

    llm_risk = data.get("risk_score")
    try:
        risk_score = max(rule_result.risk_score, min(100, int(llm_risk)))
    except (TypeError, ValueError):
        risk_score = rule_result.risk_score

    return DriftAnalysisResult(
        risk_score=risk_score, ai_summary=ai_summary, findings=findings,
        llm_applied=True, llm_error=None, cli_diff=cli_diff,
    )


_SCORERS: dict[str, RiskScorer] = {}


def _get_scorer() -> RiskScorer:
    backend = settings.RISK_ENGINE_BACKEND
    if backend not in _SCORERS:
        if backend == "llm":
            _SCORERS[backend] = LLMScorer()
        else:
            _SCORERS[backend] = RuleBasedScorer()
    return _SCORERS[backend]


def analyze(
    proposed_config: str,
    current_config: str | None = None,
    other_device_configs: dict[str, str] | None = None,
    llm_timeout: float | None = None,
) -> RiskAnalysisResult:
    """Entrypoint every caller uses. Backend-agnostic -- swapping
    `settings.RISK_ENGINE_BACKEND` changes behavior without touching any
    caller of this function. `llm_timeout` lets a caller override
    settings.OLLAMA_TIMEOUT_SECONDS/ANTHROPIC_TIMEOUT_SECONDS for this one
    call -- see create_change_request, which passes a short interactive
    budget so a slow/unreachable model degrades to the rule-based score
    in seconds instead of leaving the submitting browser tab hanging for
    however long the configured steady-state timeout is."""
    return _get_scorer().score(proposed_config, current_config, other_device_configs, llm_timeout=llm_timeout)


def is_critical(risk: RiskAnalysisResult) -> bool:
    return risk.classification == "Critical Risk"