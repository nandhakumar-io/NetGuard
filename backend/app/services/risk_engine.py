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

# (regex pattern, weight, human-readable finding)
RISK_RULES: list[tuple[str, int, str]] = [
    (r"\bno\s+router\s+bgp\b", 35, "BGP process removal detected"),
    (r"\bno\s+neighbor\s+\S+", 30, "BGP neighbor removal detected"),
    (r"\bno\s+router\s+ospf\b", 30, "OSPF process removal detected"),
    (r"\bshutdown\b", 20, "Interface shutdown detected"),
    (r"\bip\s+route\s+0\.0\.0\.0\s+0\.0\.0\.0\b", 15, "Default route change detected"),
    (r"\bno\s+ip\s+route\b", 15, "Static route removal detected"),
    (r"\baccess-list\s+\d+\s+deny\s+any\b", 15, "Broad ACL deny rule detected"),
    (r"\bno\s+vlan\s+\d+\b", 10, "VLAN removal detected"),
    (r"\bip\s+address\s+(\d{1,3}\.){3}\d{1,3}\s+255\.255\.255\.255\b", 10, "Suspicious /32 subnet mask"),
]


# ---------------------------------------------------------------------------
# Shared config parsing / network-aware checks
# ---------------------------------------------------------------------------

_IFACE_RE = re.compile(r"^interface\s+(\S+)", re.I | re.M)
_IP_ADDR_RE = re.compile(r"ip\s+address\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})", re.I)
_VLAN_DECL_RE = re.compile(r"^vlan\s+(\d+)(?:\s*\n\s*name\s+(\S+))?", re.I | re.M)
_SWITCHPORT_ACCESS_VLAN_RE = re.compile(r"switchport\s+access\s+vlan\s+(\d+)", re.I)
_NO_VLAN_RE = re.compile(r"\bno\s+vlan\s+(\d+)\b", re.I)
_ROUTE_RE = re.compile(
    r"\bip\s+route\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\S+)", re.I
)
_ACL_RULE_RE = re.compile(
    r"access-list\s+(\d+)\s+(permit|deny)\s+(\S+)(?:\s+(\S+))?(?:\s+(\S+))?", re.I
)
_OSPF_NETWORK_RE = re.compile(
    r"network\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})\s+area\s+(\S+)", re.I
)
_OSPF_PASSIVE_RE = re.compile(r"passive-interface\s+(\S+)", re.I)
_OSPF_PROCESS_RE = re.compile(r"router\s+ospf\s+\d+", re.I)


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
    return parsed


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
    ) -> RiskAnalysisResult:
        raise NotImplementedError


class RuleBasedScorer(RiskScorer):
    """v1 scorer: weighted regex rules + the network-aware checks above.
    Deterministic and dependency-free -- the default backend."""

    def score(
        self,
        proposed_config: str,
        current_config: str | None = None,
        other_device_configs: dict[str, str] | None = None,
    ) -> RiskAnalysisResult:
        findings: list[str] = []
        score = 0
        text = proposed_config.lower()

        for pattern, weight, message in RISK_RULES:
            if re.search(pattern, text):
                findings.append(message)
                score += weight

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
    """Optional ML/LLM-backed scorer, selected via
    `settings.RISK_ENGINE_BACKEND = "llm"`. Runs the same deterministic
    NetworkAwareChecks first (so hard conflicts are never missed even if the
    model call fails or is unavailable), then asks the model to reason about
    softer/contextual risk and merges its findings in. Falls back to the
    rule-based score alone if no API key is configured or the call errors,
    so enabling this backend can never make analysis unavailable.
    """

    def __init__(self) -> None:
        self._rule_scorer = RuleBasedScorer()

    def score(
        self,
        proposed_config: str,
        current_config: str | None = None,
        other_device_configs: dict[str, str] | None = None,
    ) -> RiskAnalysisResult:
        base = self._rule_scorer.score(proposed_config, current_config, other_device_configs)

        if not settings.ANTHROPIC_API_KEY:
            return base

        try:
            extra_findings, extra_score = self._call_llm(proposed_config, current_config)
        except Exception:
            # Never let a downstream model outage block change analysis --
            # degrade to the deterministic rule-based result.
            return base

        findings = base.findings + extra_findings
        score = min(base.risk_score + extra_score, 100)
        classification, recommendation = _classify(score)
        return RiskAnalysisResult(
            risk_score=score, classification=classification, recommendation=recommendation, findings=findings
        )

    def _call_llm(self, proposed_config: str, current_config: str | None) -> tuple[list[str], int]:
        import json
        import requests

        prompt = (
            "You are a network change-risk reviewer. Given the proposed device "
            "config (and current config, if provided), identify additional risks "
            "not already obvious from simple keyword matching. Respond ONLY with "
            'JSON: {"findings": ["..."], "additional_risk_points": <0-30 int>}.\n\n'
            f"CURRENT CONFIG:\n{current_config or '(none provided)'}\n\n"
            f"PROPOSED CONFIG:\n{proposed_config}"
        )
        resp = requests.post(
         f"{settings.LOCAL_LLM_BASE_URL}/chat/completions",
         json={
                "model": settings.LOCAL_LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=30,
        )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    return list(data.get("findings", [])), int(data.get("additional_risk_points", 0))


def _classify(score: int) -> tuple[str, str]:
    if score <= settings.RISK_LOW_MAX:
        return "Low Risk", "Safe to Deploy"
    elif score <= settings.RISK_MEDIUM_MAX:
        return "Medium Risk", "Review Recommended Before Deploy"
    return "Critical Risk", "Deployment Not Recommended -- Dual Approval Required"


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
) -> RiskAnalysisResult:
    """Entrypoint every caller uses. Backend-agnostic -- swapping
    `settings.RISK_ENGINE_BACKEND` changes behavior without touching any
    caller of this function."""
    return _get_scorer().score(proposed_config, current_config, other_device_configs)


def is_critical(risk: RiskAnalysisResult) -> bool:
    return risk.classification == "Critical Risk"