"""Human-readable formatting for raw protocol output.

Two problems this solves:

1. NETCONF's <get-config> reply is raw, unindented XML (Cisco IOS-XE
   native/YANG models nest 6-8 levels deep) -- readable to a parser, not
   to a person. `pretty_xml` re-indents it without changing a single tag
   or value, so the Configuration tab can show something a human can
   actually scan instead of one long line-wrapped blob.

2. `ProtocolManager.get_interfaces()` (protocol_manager.py) already
   fetches real per-interface data -- it was just never parsed into
   anything a UI could render, and never wired to an endpoint. Three
   protocols, three different wire formats:
     - NETCONF  -> XML (ietf-interfaces or Cisco native <interface> list)
     - RESTCONF -> JSON string (ietf-interfaces:interfaces)
     - SSH      -> Python repr() of NAPALM's get_interfaces() dict
   `parse_interfaces` normalizes all three into one common shape so the
   Interfaces tab can show real admin/oper status + IP addresses instead
   of only fleet-aggregate SNMP counters.
"""
from __future__ import annotations

import ast
import json
import re
import xml.dom.minidom
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET


def looks_like_xml(raw: str | None) -> bool:
    return bool(raw and raw.strip().startswith("<"))


def pretty_xml(raw: str | None) -> str | None:
    """Re-indents raw XML for display. Returns None (not an error string)
    if `raw` isn't parseable XML, so callers can fall back to showing the
    original text rather than an ugly traceback-derived message."""
    if not looks_like_xml(raw):
        return None
    try:
        parsed = xml.dom.minidom.parseString(raw)
    except Exception:
        return None
    # minidom.toprettyxml() litters blank lines between every element;
    # strip them so the output isn't twice as tall as it needs to be.
    pretty = parsed.toprettyxml(indent="  ")
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines)


@dataclass
class InterfaceStatus:
    name: str
    description: str | None = None
    admin_status: str | None = None  # "up" | "down" | None (unknown)
    oper_status: str | None = None
    ip_addresses: list[str] = field(default_factory=list)
    mtu: int | None = None
    speed: str | None = None
    mac_address: str | None = None


# Namespaces seen in practice on IOS-XE / Junos / EOS <get-config> replies.
# Both ietf-interfaces (standard YANG) and Cisco's native model are tried;
# whichever one actually matches the document wins.
_NS_STRIP = re.compile(r"\{[^}]*\}")


def _local(tag: str) -> str:
    """Strips the XML namespace off a tag, e.g. '{urn:...}interface' -> 'interface'."""
    return _NS_STRIP.sub("", tag)


def _text(elem: ET.Element | None) -> str | None:
    return elem.text.strip() if elem is not None and elem.text else None


def _find_local(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _findall_local(elem: ET.Element, name: str):
    return [child for child in elem if _local(child.tag) == name]


def _parse_interfaces_xml(raw: str) -> list[InterfaceStatus]:
    root = ET.fromstring(raw)
    results: list[InterfaceStatus] = []

    # ietf-interfaces: /rpc-reply/data/interfaces/interface[name,description,
    # enabled,oper-status,ipv4/address/ip]
    for interfaces_container in root.iter():
        if _local(interfaces_container.tag) != "interfaces":
            continue
        for iface in _findall_local(interfaces_container, "interface"):
            name = _text(_find_local(iface, "name"))
            if not name:
                continue
            enabled_elem = _find_local(iface, "enabled")
            oper_elem = _find_local(iface, "oper-status")
            status = InterfaceStatus(
                name=name,
                description=_text(_find_local(iface, "description")),
                admin_status=(
                    {"true": "up", "false": "down"}.get(_text(enabled_elem) or "")
                    if enabled_elem is not None
                    else None
                ),
                oper_status=_text(oper_elem),
                mtu=int(_text(_find_local(iface, "mtu")) or 0) or None,
            )
            for proto_name in ("ipv4", "ipv6"):
                proto_elem = _find_local(iface, proto_name)
                if proto_elem is None:
                    continue
                for addr in _findall_local(proto_elem, "address"):
                    ip = _text(_find_local(addr, "ip"))
                    prefix = _text(_find_local(addr, "prefix-length"))
                    if ip:
                        status.ip_addresses.append(f"{ip}/{prefix}" if prefix else ip)
            results.append(status)
        if results:
            return results

    # Cisco native: .../native/interface/<Type>/[name, description,
    # shutdown, ip/address/primary/address+mask]
    for native in root.iter():
        if _local(native.tag) != "interface":
            continue
        for type_elem in native:
            type_name = _local(type_elem.tag)
            if type_name in ("name", "description"):  # not an interface-type container
                continue
            for iface in list(type_elem) if _find_local(type_elem, "name") is None else [type_elem]:
                num = _text(_find_local(iface, "name"))
                if not num:
                    continue
                shutdown = _find_local(iface, "shutdown") is not None
                status = InterfaceStatus(
                    name=f"{type_name}{num}",
                    description=_text(_find_local(iface, "description")),
                    admin_status="down" if shutdown else "up",
                )
                ip_elem = _find_local(iface, "ip")
                addr_elem = _find_local(ip_elem, "address") if ip_elem is not None else None
                primary = _find_local(addr_elem, "primary") if addr_elem is not None else None
                if primary is not None:
                    ip = _text(_find_local(primary, "address"))
                    mask = _text(_find_local(primary, "mask"))
                    if ip:
                        status.ip_addresses.append(f"{ip}/{mask}" if mask else ip)
                results.append(status)

    return results


def _parse_interfaces_json(raw: str) -> list[InterfaceStatus]:
    data = json.loads(raw)
    container = data.get("ietf-interfaces:interfaces") or data.get("interfaces") or data
    entries = container.get("interface", []) if isinstance(container, dict) else []
    results = []
    for iface in entries:
        status = InterfaceStatus(
            name=iface.get("name", "?"),
            description=iface.get("description"),
            admin_status="up" if iface.get("enabled", True) else "down",
            oper_status=iface.get("oper-status") or iface.get("admin-status"),
        )
        for addr in (iface.get("ietf-ip:ipv4", {}) or {}).get("address", []):
            ip = addr.get("ip")
            prefix = addr.get("prefix-length")
            if ip:
                status.ip_addresses.append(f"{ip}/{prefix}" if prefix else ip)
        results.append(status)
    return results


def _parse_interfaces_napalm_repr(raw: str) -> list[InterfaceStatus]:
    """NAPALM's get_interfaces() dict, currently stored via str(facts) --
    valid Python literal syntax, so ast.literal_eval (not eval) is safe
    and exact, unlike a JSON parse which would fail on the single-quoted
    Python repr."""
    data = ast.literal_eval(raw)
    if not isinstance(data, dict):
        return []
    results = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        results.append(
            InterfaceStatus(
                name=name,
                description=info.get("description") or None,
                admin_status="up" if info.get("is_enabled") else "down",
                oper_status="up" if info.get("is_up") else "down",
                mtu=info.get("mtu"),
                speed=f"{info['speed']} Mbps" if info.get("speed") else None,
                mac_address=info.get("mac_address") or None,
            )
        )
    return results


def parse_interfaces(raw: str | None, protocol: str) -> list[InterfaceStatus]:
    """Best-effort normalization; returns [] (never raises) on anything
    that doesn't parse cleanly -- a malformed/unexpected payload should
    degrade to 'no interfaces parsed', not a 500 on the Interfaces tab."""
    if not raw:
        return []
    try:
        if protocol == "netconf":
            return _parse_interfaces_xml(raw)
        if protocol == "restconf":
            return _parse_interfaces_json(raw)
        return _parse_interfaces_napalm_repr(raw)
    except Exception:
        return []