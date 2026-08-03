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
from xml.sax.saxutils import escape as _xml_escape


def looks_like_xml(raw: str | None) -> bool:
    return bool(raw and raw.strip().startswith("<"))


def strip_rpc_envelope(raw: str | None) -> str | None:
    """Strips the outer <rpc-reply>/<data> NETCONF envelope from a
    <get-config> reply, returning just the actual config content (e.g.
    the <native>...</native> subtree) serialized back to XML.

    ncclient sets the <rpc-reply> `message-id` attribute to a fresh
    random `urn:uuid:...` value on *every single RPC call* -- storing the
    full envelope (as ConfigSnapshot/GoldenConfig baselines and the live
    read used for drift comparison all did) meant two reads of the exact
    same, completely unchanged device config always differed by at least
    that one line. In practice this showed up as every drift scan / CR
    diff rendering the *entire* config as "added"/"removed" around
    whatever the real one- or two-line change was (see the Drift page's
    "756 line(s) added, 2 line(s) removed" for a single assigned IP), and
    it also meant the risk/drift LLM prompts got a needlessly huge, mostly
    boilerplate blob to reason over -- a real contributor to the local
    Ollama pass timing out.

    Returns the original text unchanged if it doesn't look like an
    rpc-reply/data envelope (nothing to strip -- e.g. this is already
    inner content, or non-XML CLI text) or fails to parse for any reason;
    callers should never depend on stripping actually happening.
    """
    if not looks_like_xml(raw):
        return raw
    try:
        from lxml import etree as _lxml_etree

        root = _lxml_etree.fromstring(raw.encode("utf-8"))
    except Exception:
        return raw
    if _local(root.tag) != "rpc-reply":
        return raw
    data = next((child for child in root if _local(child.tag) == "data"), None)
    if data is None:
        return raw
    children = list(data)
    if not children:
        return raw
    try:
        return "".join(
            _lxml_etree.tostring(child, encoding="unicode") for child in children
        )
    except Exception:
        return raw


def pretty_xml(raw: str | None) -> str | None:
    """Re-indents raw XML for display. Returns None (not an error string)
    if `raw` isn't parseable XML, so callers can fall back to showing the
    original text rather than an ugly traceback-derived message."""
    if not looks_like_xml(raw):
        return None
    try:
        parsed = xml.dom.minidom.parseString(raw)
    except Exception:
        # Might be multiple root elements because of strip_rpc_envelope.
        try:
            parsed = xml.dom.minidom.parseString(f"<config_data>\n{raw}\n</config_data>")
        except Exception:
            return None
    
    # minidom.toprettyxml() litters blank lines between every element;
    # strip them so the output isn't twice as tall as it needs to be.
    pretty = parsed.toprettyxml(indent="  ")
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines)


def cli_to_netconf_config(cli_text: str) -> str:
    """Wraps plain IOS-style CLI lines (the format every change request in
    this app is authored, validated (validation_engine), diffed
    (diff_engine), and risk-scored (risk_engine) in -- there is no
    separate "XML mode") as a NETCONF <config> payload IOS-XE will
    actually accept.

    Without this, `proposed_config` (plain CLI text, e.g. "interface
    GigabitEthernet0/3\\nshutdown") was being handed directly to
    ncclient's edit_config() as the NETCONF `config` payload, which
    ncclient/lxml tries to parse as XML and rejects immediately with
    "Start tag expected, '<' not found" -- CLI text was never going to
    parse as XML, this isn't a formatting bug, the payload was simply
    the wrong shape for the protocol.

    Rather than hand-translating every supported CLI command
    (validation_engine's allow-list alone covers ~50 first-word verbs)
    into per-leaf Cisco-IOS-XE-native YANG XML -- a large, fragile
    surface that would silently drift out of sync with that allow-list --
    this uses IOS-XE's documented `cli-config-data` NETCONF extension
    (namespace `http://cisco.com/yang/cisco-ia`), which takes a list of
    literal CLI lines and applies them exactly as if typed at the
    terminal, sub-mode context (e.g. `shutdown` after `interface
    GigabitEthernet0/3`) included. This is Cisco-specific -- callers
    should only use this for vendor="cisco" devices; other NETCONF
    vendors need their own equivalent (Junos: `load-configuration
    format text`) rather than silently sending this and failing anyway.
    """
    cmds = [
        line.strip() for line in cli_text.splitlines()
        if line.strip() and not line.strip().startswith(("!", "#"))
    ]
    cmd_xml = "".join(f"<cmd>{_xml_escape(c)}</cmd>" for c in cmds)
    # <config> needs an *explicit* NETCONF base namespace here, even
    # though it's ultimately nested inside an <rpc> element that's
    # already in that namespace. ncclient parses this string into its
    # own standalone lxml Element (via to_ele()) before appending it as
    # a child of the <edit-config> node; lxml elements built from a
    # separate document don't inherit the parent tree's default
    # namespace on append, so without this, the serialized RPC came out
    # as <config xmlns="">...</config> -- a *different* (empty) namespace
    # than the one edit-config's schema expects for its <config> child.
    # IOS-XE devices enforce that strictly and rejected the whole push
    # with an rpc-error: tag "unknown-element", bad-element "config",
    # path "/rpc/edit-config" -- the device wasn't objecting to the
    # cli-config-data content at all, it never recognized the wrapping
    # <config> element itself as the one the schema requires.
    return (
        '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        f'<cli-config-data xmlns="http://cisco.com/yang/cisco-ia">{cmd_xml}</cli-config-data>'
        "</config>"
    )


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
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # strip_rpc_envelope() might return multiple root elements (e.g. <native> and <interfaces>) 
        # which isn't valid XML as a single string. Wrap them cleanly.
        try:
            root = ET.fromstring(f"<data>{raw}</data>")
        except ET.ParseError:
            return []
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


# ---------------------------------------------------------------------------
# Structural XML diff + CLI-command / human-readable translation
# ---------------------------------------------------------------------------
# These three functions are called by:
#   - app.api.change_requests._score_change  (CR submission / rescore)
#   - app.services.risk_engine.analyze_drift  (drift scan AI summary)
#   - app.services.drift_service.detect_drift (cli_diff column on ConfigDrift)
# They convert NETCONF-style XML configs into structured diffs, then into
# either IOS CLI-equivalent commands or plain-English summaries so the Drift
# page and Change Requests page can show human-readable diffs instead of raw
# XML lines.

@dataclass
class StructuralChange:
    """One atomic difference between two XML configs."""
    path: list[str]          # element ancestry, e.g. ["native", "interface", "GigabitEthernet", "0/1"]
    action: str              # "added" | "removed" | "modified"
    element: str             # leaf tag name, e.g. "shutdown", "address", "name"
    old_value: str | None = None
    new_value: str | None = None


def _xml_to_dict(elem: ET.Element, depth: int = 0, max_depth: int = 15) -> dict:
    """Recursively converts an XML element into a nested dict.
    Leaf elements (text content, no children) become {tag: text}.
    Container elements become {tag: {child_tag: ...}}.
    Repeated tags at the same level are collected into lists."""
    if depth > max_depth:
        return {}
    result: dict = {}
    for child in elem:
        tag = _local(child.tag)
        children_of_child = list(child)
        if children_of_child:
            value = _xml_to_dict(child, depth + 1, max_depth)
        else:
            value = (child.text or "").strip()
        if tag in result:
            existing = result[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[tag] = [existing, value]
        else:
            result[tag] = value
    return result


def _diff_dicts(
    old: dict | str | list,
    new: dict | str | list,
    path: list[str],
    changes: list[StructuralChange],
) -> None:
    """Recursively diffs two nested dicts produced by _xml_to_dict."""
    if isinstance(old, str) and isinstance(new, str):
        if old != new:
            elem_name = path[-1] if path else "value"
            changes.append(StructuralChange(
                path=path[:-1], action="modified", element=elem_name,
                old_value=old, new_value=new,
            ))
        return
    if isinstance(old, str) or isinstance(new, str):
        # Type mismatch (leaf vs container) -- treat as replaced
        elem_name = path[-1] if path else "value"
        changes.append(StructuralChange(
            path=path[:-1], action="modified", element=elem_name,
            old_value=str(old) if not isinstance(old, dict) else None,
            new_value=str(new) if not isinstance(new, dict) else None,
        ))
        return
    if isinstance(old, list) or isinstance(new, list):
        old_list = old if isinstance(old, list) else ([old] if old else [])
        new_list = new if isinstance(new, list) else ([new] if new else [])
        # Simple positional diff for list items
        for i, item in enumerate(new_list):
            if i < len(old_list):
                _diff_dicts(old_list[i], item, path, changes)
            else:
                _collect_added(item, path, changes)
        for i in range(len(new_list), len(old_list)):
            _collect_removed(old_list[i], path, changes)
        return

    # Both are dicts
    old_d = old if isinstance(old, dict) else {}
    new_d = new if isinstance(new, dict) else {}
    all_keys = dict.fromkeys(list(old_d.keys()) + list(new_d.keys()))
    for key in all_keys:
        if key in old_d and key in new_d:
            _diff_dicts(old_d[key], new_d[key], path + [key], changes)
        elif key in new_d:
            _collect_added(new_d[key], path + [key], changes)
        else:
            _collect_removed(old_d[key], path + [key], changes)


def _collect_added(value: dict | str | list, path: list[str], changes: list[StructuralChange]) -> None:
    elem_name = path[-1] if path else "element"
    if isinstance(value, str):
        changes.append(StructuralChange(
            path=path[:-1], action="added", element=elem_name, new_value=value,
        ))
    elif isinstance(value, dict):
        # An entire container was added -- emit one change per leaf inside it
        has_leaves = False
        for k, v in value.items():
            if isinstance(v, str):
                changes.append(StructuralChange(
                    path=path, action="added", element=k, new_value=v,
                ))
                has_leaves = True
            elif isinstance(v, dict) or isinstance(v, list):
                _collect_added(v, path + [k], changes)
                has_leaves = True
        if not has_leaves:
            # Empty container / flag element (like <shutdown/>)
            changes.append(StructuralChange(
                path=path[:-1], action="added", element=elem_name,
            ))
    elif isinstance(value, list):
        for item in value:
            _collect_added(item, path, changes)


def _collect_removed(value: dict | str | list, path: list[str], changes: list[StructuralChange]) -> None:
    elem_name = path[-1] if path else "element"
    if isinstance(value, str):
        changes.append(StructuralChange(
            path=path[:-1], action="removed", element=elem_name, old_value=value,
        ))
    elif isinstance(value, dict):
        has_leaves = False
        for k, v in value.items():
            if isinstance(v, str):
                changes.append(StructuralChange(
                    path=path, action="removed", element=k, old_value=v,
                ))
                has_leaves = True
            elif isinstance(v, dict) or isinstance(v, list):
                _collect_removed(v, path + [k], changes)
                has_leaves = True
        if not has_leaves:
            changes.append(StructuralChange(
                path=path[:-1], action="removed", element=elem_name,
            ))
    elif isinstance(value, list):
        for item in value:
            _collect_removed(item, path, changes)


def xml_structural_diff(
    config_a: str | None, config_b: str | None,
) -> list[StructuralChange] | None:
    """Computes a structural diff between two XML configs. Returns None
    (not an empty list) if either side isn't XML -- callers use this to
    decide whether to fall back to the raw unified-diff display."""
    if not looks_like_xml(config_a) or not looks_like_xml(config_b):
        return None
    try:
        root_a = ET.fromstring(config_a)
        root_b = ET.fromstring(config_b)
    except ET.ParseError:
        return None

    dict_a = _xml_to_dict(root_a)
    dict_b = _xml_to_dict(root_b)
    changes: list[StructuralChange] = []
    _diff_dicts(dict_a, dict_b, [], changes)
    return changes


# --- CLI command translation -----------------------------------------------
# Maps structural XML paths to Cisco IOS CLI command patterns. The mapping
# covers the common Cisco IOS-XE native YANG model paths that show up in
# NETCONF <get-config> replies; anything not specifically mapped gets a
# generic "path > element: value" fallback that's still more readable than
# raw XML tags.

_INTERFACE_TYPES = {
    "GigabitEthernet", "FastEthernet", "Loopback", "Vlan",
    "TenGigabitEthernet", "Port-channel", "Tunnel",
}


def _path_contains_interface(path: list[str]) -> tuple[str, str] | None:
    """Detects if the path walks through an interface container.

    Cisco IOS-XE NETCONF XML has interface paths like:
      ['interface', 'GigabitEthernet', ...]  (type is a path segment)
      ['native', 'interface', 'GigabitEthernet', '0/1', ...]
    The interface number/name may be the next path segment or may only
    exist as a leaf 'name' element inside the dict. We return
    (type, number) when found, or (type, '') when the number isn't in
    the path."""
    for i, segment in enumerate(path):
        if segment in _INTERFACE_TYPES:
            # Next path segment might be the interface number (e.g. "0/1")
            if i + 1 < len(path) and path[i + 1] not in _INTERFACE_TYPES:
                num = path[i + 1]
                # Skip non-number-like segments (e.g. "ip", "shutdown")
                if num and (num[0].isdigit() or "/" in num):
                    return segment, num
            return segment, ""
    return None


def to_cli_commands(changes: list[StructuralChange] | None) -> list[str]:
    """Converts structural changes into IOS CLI-equivalent command lines."""
    if not changes:
        return []
    lines: list[str] = []
    # Group changes by interface context for cleaner output
    iface_changes: dict[str, list[StructuralChange]] = {}
    global_changes: list[StructuralChange] = []

    for ch in changes:
        iface = _path_contains_interface(ch.path)
        if iface:
            key = f"{iface[0]}{iface[1]}"
            iface_changes.setdefault(key, []).append(ch)
        else:
            global_changes.append(ch)

    # Emit interface-grouped commands
    for iface_name, iface_chs in iface_changes.items():
        lines.append(f"interface {iface_name}")
        for ch in iface_chs:
            cmd = _change_to_cli_subcommand(ch)
            if cmd:
                lines.append(f"  {cmd}")

    # Emit global commands
    for ch in global_changes:
        cmd = _change_to_cli_global(ch)
        if cmd:
            lines.append(cmd)

    return lines


def _change_to_cli_subcommand(ch: StructuralChange) -> str | None:
    """Translates one structural change under an interface context to an
    IOS CLI sub-command string."""
    el = ch.element.lower()
    prefix = "no " if ch.action == "removed" else ""

    if el == "shutdown":
        if ch.action == "added":
            return "shutdown"
        elif ch.action == "removed":
            return "no shutdown"
        return None
    elif el == "address" and "ip" in [p.lower() for p in ch.path]:
        val = ch.new_value or ch.old_value or ""
        # Try to find mask in sibling changes (simplified)
        return f"{prefix}ip address {val}" if val else None
    elif el == "mask":
        return None  # handled alongside address
    elif el == "description":
        if ch.action == "removed":
            return "no description"
        return f"description {ch.new_value}" if ch.new_value else None
    elif el == "name":
        return None  # interface name itself, not a command
    elif el in ("mtu",):
        if ch.action == "removed":
            return "no mtu"
        return f"mtu {ch.new_value}" if ch.new_value else None
    elif el == "negotiation" or el == "auto":
        return None  # noise
    else:
        val = ch.new_value or ch.old_value or ""
        readable_el = el.replace("-", " ").replace("_", " ")
        if ch.action == "removed":
            return f"no {readable_el}" + (f" {val}" if val else "")
        return f"{readable_el} {val}".strip() if val else readable_el


def _change_to_cli_global(ch: StructuralChange) -> str | None:
    """Translates a non-interface structural change to a global CLI command."""
    el = ch.element.lower()
    prefix = "no " if ch.action == "removed" else ""
    val = ch.new_value or ch.old_value or ""

    if el == "hostname":
        if ch.action == "removed":
            return None  # can't remove hostname
        return f"hostname {val}" if val else None
    elif el == "domain-name" or el == "domain" and "name" in el:
        return f"{prefix}ip domain-name {val}" if val else None
    elif "cdp" in el or "cdp" in " ".join(ch.path).lower():
        if el == "run" or el == "cdp":
            if ch.action == "removed":
                return "no cdp run"
            return "cdp run"
        return f"{prefix}cdp {el} {val}".strip()
    elif el == "secret" or el == "password":
        return f"{prefix}enable {el} ****" if val else None
    elif "vlan" in " ".join(ch.path).lower():
        if el == "name":
            return None  # VLAN name under a vlan context
        vlan_id = val or ""
        return f"{prefix}vlan {vlan_id}".strip() if vlan_id else None
    elif "route" in " ".join(ch.path).lower():
        return f"{prefix}ip route {val}".strip() if val else None
    elif "access-list" in el or "acl" in el:
        return f"{prefix}access-list {val}".strip() if val else None
    else:
        # Generic fallback: path > element: value
        path_str = " > ".join(p for p in ch.path if p not in ("native", "config"))
        readable_el = el.replace("-", " ").replace("_", " ")
        if path_str:
            base = f"{path_str} > {readable_el}"
        else:
            base = readable_el

        if ch.action == "removed":
            return f"no {base}" + (f" {val}" if val else "")
        elif ch.action == "modified":
            return f"{base}: {ch.old_value} → {ch.new_value}"
        return f"{base} {val}".strip() if val else base


def humanize_structural_diff(
    changes: list[StructuralChange] | None,
) -> list[str]:
    """Produces plain-English descriptions of each structural change."""
    if not changes:
        return []
    bullets: list[str] = []
    seen: set[str] = set()  # dedup

    for ch in changes:
        desc = _humanize_one_change(ch)
        if desc and desc not in seen:
            seen.add(desc)
            bullets.append(desc)
        if len(bullets) >= 30:
            bullets.append(f"...and {len(changes) - 30} more changes")
            break
    return bullets


def _humanize_one_change(ch: StructuralChange) -> str | None:
    """Plain-English description of one structural change."""
    el = ch.element.lower()
    iface = _path_contains_interface(ch.path)
    prefix = f"Interface {iface[0]}{iface[1]}" if iface else None
    action = ch.action

    if el == "shutdown":
        if prefix:
            if action == "added":
                return f"{prefix} was administratively shut down"
            return f"{prefix} was brought back up (no shutdown)"
        return "A shutdown command was " + ("added" if action == "added" else "removed")

    if el == "address" and "ip" in [p.lower() for p in ch.path]:
        if prefix:
            if action == "added":
                return f"{prefix} was assigned IP address {ch.new_value}"
            elif action == "removed":
                return f"{prefix} had IP address {ch.old_value} removed"
            return f"{prefix} IP address changed from {ch.old_value} to {ch.new_value}"
        val = ch.new_value or ch.old_value or "unknown"
        return f"IP address {val} was {'assigned' if action != 'removed' else 'removed'}"

    if el == "description":
        if prefix:
            if action == "removed":
                return f"{prefix} description was removed"
            return f"{prefix} description set to \"{ch.new_value}\""
        return None

    if el == "hostname":
        if action == "modified":
            return f"Hostname changed from '{ch.old_value}' to '{ch.new_value}'"
        elif action == "added":
            return f"Hostname set to '{ch.new_value}'"
        return f"Hostname '{ch.old_value}' was removed"

    if "cdp" in el or "cdp" in " ".join(ch.path).lower():
        if action == "removed":
            return "CDP (Cisco Discovery Protocol) was disabled"
        return "CDP (Cisco Discovery Protocol) was enabled"

    if el == "name" and iface:
        return None  # interface name, not interesting

    if el == "mask":
        return None  # shown alongside address

    # Generic fallback
    readable = el.replace("-", " ").replace("_", " ")
    context = prefix or (" > ".join(p for p in ch.path if p not in ("native", "config")) or "device")
    val = ch.new_value or ch.old_value or ""

    if action == "added":
        return f"{context}: '{readable}' set to {val}" if val else f"{context}: '{readable}' was enabled"
    elif action == "removed":
        return f"{context}: '{readable}' was removed" + (f" (was {val})" if val else "")
    return f"{context}: '{readable}' changed from {ch.old_value} to {ch.new_value}"