"""Vendor-agnostic Config Intents.

Netmiko/NAPALM already let NetGuard *push* config to Cisco IOS/IOS-XE,
NX-OS, Junos, and EOS -- but the config text itself has always had to be
authored per-vendor (or per-template, via config_template.py's Jinja2
templates rendering a single vendor's syntax).

This module adds one more layer above that: a small set of structured,
declarative "intents" (add a VLAN, add an ACL rule, set an interface
description, ...) that get *translated* into the right CLI syntax for
whatever vendor a target device actually runs. An engineer/admin (or the
API caller building a multi-device change request) states the change
once -- "VLAN 100 named GUEST_WIFI" -- and NetGuard renders the
IOS/NX-OS, Junos, and EOS equivalents, so a mixed-vendor change request
doesn't require hand-writing three dialects of the same one-line change.

Deliberately scoped to a handful of common, high-frequency intents
rather than trying to be a general network-config DSL -- for anything
this doesn't cover, config_template.py's raw per-vendor templates are
still the right tool.
"""
import dataclasses
import enum
from typing import Any


class Vendor(str, enum.Enum):
    CISCO_IOS = "cisco_ios"     # also covers IOS-XE
    CISCO_NXOS = "cisco_nxos"
    JUNIPER_JUNOS = "juniper_junos"
    ARISTA_EOS = "arista_eos"


# Device.vendor ("cisco" / "juniper" / "arista" / "linux") doesn't
# distinguish IOS from NX-OS -- both are "cisco" at the inventory level.
# Intent rendering needs that finer distinction, so callers pass an
# explicit Vendor (defaulting to IOS-style for "cisco", which is the
# large majority case) rather than this module guessing from the model.
DEVICE_MODEL_VENDOR_DEFAULT = {
    "cisco": Vendor.CISCO_IOS,
    "juniper": Vendor.JUNIPER_JUNOS,
    "arista": Vendor.ARISTA_EOS,
}


class IntentKind(str, enum.Enum):
    ADD_VLAN = "add_vlan"
    ADD_ACL_RULE = "add_acl_rule"
    SET_INTERFACE_DESCRIPTION = "set_interface_description"
    SET_INTERFACE_ADMIN_STATE = "set_interface_admin_state"
    ADD_STATIC_ROUTE = "add_static_route"
    SET_NTP_SERVER = "set_ntp_server"


@dataclasses.dataclass
class ConfigIntent:
    kind: IntentKind
    params: dict[str, Any]


class UnsupportedIntentError(Exception):
    """Raised when a (kind, vendor) combination has no renderer -- e.g.
    an intent kind that doesn't make sense for a given vendor, or one
    that hasn't been implemented yet. Always resolvable by falling back
    to a hand-written per-vendor config_template for that one change."""


def _require(params: dict, *names: str) -> None:
    missing = [n for n in names if n not in params or params[n] in (None, "")]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")


# --- add_vlan ---------------------------------------------------------

def _render_add_vlan(vendor: Vendor, p: dict) -> str:
    _require(p, "vlan_id", "name")
    vlan_id, name = p["vlan_id"], p["name"]
    if vendor in (Vendor.CISCO_IOS, Vendor.CISCO_NXOS):
        return f"vlan {vlan_id}\n name {name}\n"
    if vendor == Vendor.ARISTA_EOS:
        return f"vlan {vlan_id}\n   name {name}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        return f"set vlans {name} vlan-id {vlan_id}"
    raise UnsupportedIntentError(f"add_vlan is not supported for {vendor.value}")


# --- add_acl_rule -------------------------------------------------------
# params: acl_name, action ("permit"/"deny"), protocol, source, destination,
# sequence (optional)

def _render_add_acl_rule(vendor: Vendor, p: dict) -> str:
    _require(p, "acl_name", "action", "protocol", "source", "destination")
    acl_name = p["acl_name"]
    action = p["action"].lower()
    if action not in ("permit", "deny"):
        raise ValueError("action must be 'permit' or 'deny'")
    protocol, source, destination = p["protocol"], p["source"], p["destination"]
    sequence = p.get("sequence")

    if vendor == Vendor.CISCO_IOS:
        seq = f"{sequence} " if sequence else ""
        return f"ip access-list extended {acl_name}\n {seq}{action} {protocol} {source} {destination}\n"
    if vendor == Vendor.CISCO_NXOS:
        seq = f"{sequence} " if sequence else ""
        return f"ip access-list {acl_name}\n {seq}{action} {protocol} {source} {destination}\n"
    if vendor == Vendor.ARISTA_EOS:
        seq = f"{sequence} " if sequence else ""
        return f"ip access-list {acl_name}\n   {seq}{action} {protocol} {source} {destination}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        # Junos firewall filters are term-based rather than sequence-numbered;
        # `sequence` (if given) is used as the term name for a stable,
        # human-readable identity across renders of the same intent.
        term = f"term seq-{sequence}" if sequence else f"term {source}-to-{destination}".replace("/", "_")
        junos_action = "accept" if action == "permit" else "discard"
        return (
            f"set firewall filter {acl_name} {term} from source-address {source}\n"
            f"set firewall filter {acl_name} {term} from destination-address {destination}\n"
            f"set firewall filter {acl_name} {term} from protocol {protocol}\n"
            f"set firewall filter {acl_name} {term} then {junos_action}"
        )
    raise UnsupportedIntentError(f"add_acl_rule is not supported for {vendor.value}")


# --- set_interface_description ------------------------------------------

def _render_set_interface_description(vendor: Vendor, p: dict) -> str:
    _require(p, "interface", "description")
    interface, description = p["interface"], p["description"]
    if vendor in (Vendor.CISCO_IOS, Vendor.CISCO_NXOS):
        return f"interface {interface}\n description {description}\n"
    if vendor == Vendor.ARISTA_EOS:
        return f"interface {interface}\n   description {description}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        return f'set interfaces {interface} description "{description}"'
    raise UnsupportedIntentError(f"set_interface_description is not supported for {vendor.value}")


# --- set_interface_admin_state -------------------------------------------
# params: interface, enabled (bool)

def _render_set_interface_admin_state(vendor: Vendor, p: dict) -> str:
    _require(p, "interface", "enabled")
    interface, enabled = p["interface"], bool(p["enabled"])
    if vendor in (Vendor.CISCO_IOS, Vendor.CISCO_NXOS):
        return f"interface {interface}\n {'no shutdown' if enabled else 'shutdown'}\n"
    if vendor == Vendor.ARISTA_EOS:
        return f"interface {interface}\n   {'no shutdown' if enabled else 'shutdown'}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        return (
            f"delete interfaces {interface} disable"
            if enabled
            else f"set interfaces {interface} disable"
        )
    raise UnsupportedIntentError(f"set_interface_admin_state is not supported for {vendor.value}")


# --- add_static_route ------------------------------------------------

def _render_add_static_route(vendor: Vendor, p: dict) -> str:
    _require(p, "prefix", "mask", "next_hop")
    prefix, mask, next_hop = p["prefix"], p["mask"], p["next_hop"]
    if vendor == Vendor.CISCO_IOS:
        return f"ip route {prefix} {mask} {next_hop}\n"
    if vendor == Vendor.CISCO_NXOS:
        return f"ip route {prefix}/{mask} {next_hop}\n" if "/" not in str(mask) else f"ip route {prefix}/{mask} {next_hop}\n"
    if vendor == Vendor.ARISTA_EOS:
        return f"ip route {prefix} {mask} {next_hop}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        return f"set routing-options static route {prefix}/{mask} next-hop {next_hop}"
    raise UnsupportedIntentError(f"add_static_route is not supported for {vendor.value}")


# --- set_ntp_server ---------------------------------------------------

def _render_set_ntp_server(vendor: Vendor, p: dict) -> str:
    _require(p, "server_ip")
    server_ip = p["server_ip"]
    if vendor in (Vendor.CISCO_IOS, Vendor.CISCO_NXOS, Vendor.ARISTA_EOS):
        return f"ntp server {server_ip}\n"
    if vendor == Vendor.JUNIPER_JUNOS:
        return f"set system ntp server {server_ip}"
    raise UnsupportedIntentError(f"set_ntp_server is not supported for {vendor.value}")


_RENDERERS = {
    IntentKind.ADD_VLAN: _render_add_vlan,
    IntentKind.ADD_ACL_RULE: _render_add_acl_rule,
    IntentKind.SET_INTERFACE_DESCRIPTION: _render_set_interface_description,
    IntentKind.SET_INTERFACE_ADMIN_STATE: _render_set_interface_admin_state,
    IntentKind.ADD_STATIC_ROUTE: _render_add_static_route,
    IntentKind.SET_NTP_SERVER: _render_set_ntp_server,
}


def render_intent(intent: ConfigIntent, vendor: Vendor) -> str:
    """Renders one intent into the CLI snippet for `vendor`. Raises
    ValueError for missing/invalid params, or UnsupportedIntentError if
    this intent kind has no renderer for that vendor."""
    renderer = _RENDERERS.get(intent.kind)
    if renderer is None:
        raise UnsupportedIntentError(f"Unknown intent kind '{intent.kind}'")
    return renderer(vendor, intent.params)


def render_intent_for_all_vendors(intent: ConfigIntent) -> dict[str, str | None]:
    """Renders one intent for every supported vendor at once -- used to
    show an admin the "here's what this looks like on each platform"
    preview before it's attached to per-device change requests. A `None`
    value means that vendor doesn't support this intent kind (surfaced
    as an explicit gap rather than a silent omission)."""
    result: dict[str, str | None] = {}
    for vendor in Vendor:
        try:
            result[vendor.value] = render_intent(intent, vendor)
        except UnsupportedIntentError:
            result[vendor.value] = None
    return result


# Comment-line prefix per vendor, used only by build_proposed_config's
# separator below. Junos config text uses "#" for comments, not "!" --
# stamping the same "!" line used for Cisco/Arista onto a Junos payload
# means every intent appended this way would carry an invalid line
# straight into netconf_service.push_config's Junos `set`-mode path,
# which sends this text to the device close to verbatim (see
# _push_once_junos_set) and has no reason to expect or strip Cisco-style
# comments.
_COMMENT_PREFIX = {
    Vendor.CISCO_IOS: "!",
    Vendor.CISCO_NXOS: "!",
    Vendor.ARISTA_EOS: "!",
    Vendor.JUNIPER_JUNOS: "#",
}


def build_proposed_config(current_config: str | None, intent: ConfigIntent, vendor: Vendor) -> str:
    """Appends the rendered intent snippet to the device's current
    config, the same shape ChangeRequest.proposed_config expects
    elsewhere in the app (a full config body, not just a diff/snippet).
    Vendor CLIs are largely additive/idempotent for these intent kinds
    (re-applying `vlan 100` / `name X` is a no-op if already present),
    so simple append-then-let-the-device-CLI-merge is safe for the
    intents this module supports; anything needing true merge/replace
    semantics belongs in a full config_template instead.
    """
    rendered = render_intent(intent, vendor)
    base = (current_config or "").rstrip("\n")
    comment = _COMMENT_PREFIX[vendor]
    separator = f"{comment} --- NetGuard config intent: {intent.kind.value} ---"
    if not base:
        return rendered
    return f"{base}\n\n{separator}\n{rendered}"
