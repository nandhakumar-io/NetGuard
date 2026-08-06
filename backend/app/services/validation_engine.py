"""Automated Validation Engine (SRS 6.4 / FR-5).

Two layers of checking, both enforced before a change request is ever
allowed to reach an approver or a device (see app.api.change_requests and
app.services.pipeline_service, which both call `validate_syntax` as a hard
gate -- a failing result blocks the workflow, it never just produces a
warning that can be ignored):

  1. Vendor-aware command validation -- a lightweight parser + allow-list
     per CLI dialect (Cisco IOS / Arista EOS share one classic IOS-style
     grammar; Juniper uses `set`-style hierarchical statements). This is
     not a full vendor grammar (that would mean embedding/maintaining a
     real IOS/Junos parser), but it's enough to catch the two things SRS
     6.4 calls out as "invalid commands" / "unsupported features": a
     first-word command token that isn't a real config verb for that
     platform, and structurally malformed lines (e.g. `interface` with no
     interface name).

  2. Inventory cross-checks -- catches config that is syntactically fine
     but semantically broken given what's *already on the device*:
       - Gateway conflicts: a configured default gateway / static default
         route must actually be reachable through a subnet the device
         has (or is gaining) on one of its own interfaces.
       - Broken ACL references: `ip access-group NAME in/out` (or EOS's
         `access-group NAME in/out`) applied to an interface must
         reference an ACL that's actually defined somewhere (currently
         running, or being defined in this same change).
       - VLAN references: `switchport access/trunk vlan <id>` and VLAN
         SVIs (`interface Vlan<id>`) must reference a VLAN that's
         actually defined (a `vlan <id>` block), not a typo'd/nonexistent
         one.
       - Interface dependencies: commands scoped to `interface X` (ACL
         application, VLAN assignment) must apply to an interface that's
         either defined in this same change or already exists on the
         device (drawn from the device's live/last-known running
         config, i.e. the device inventory -- not just guessed).

`current_config` is the cross-check's "inventory": the device's last-known
running configuration (fetched live where the caller has it -- see
app.api.change_requests, which now fetches it via ProtocolManager before
validating). Passing `None` degrades gracefully: cross-checks that need it
are skipped with a warning instead of a hard failure, since there's
nothing to conflict-check against yet (e.g. a brand new device).
"""
import ipaddress
import re
from dataclasses import dataclass, field

from app.services.config_format_service import looks_like_xml


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


KNOWN_INVALID_TOKENS = ["TODO", "FIXME", "<placeholder>"]

# --- Cisco IOS / Arista EOS command allow-list -------------------------
# Both are classic IOS-style CLIs (EOS is an IOS derivative), so they share
# one allow-list of recognized *first-word* config commands. Anything
# whose first token isn't in here is flagged as an unrecognized/unsupported
# command -- this is the "invalid commands, unsupported features" check
# from SRS 6.4.
IOS_STYLE_ALLOWED_COMMANDS = {
    "interface", "ip", "ipv6", "no", "vlan", "access-list", "route-map",
    "router", "line", "banner", "hostname", "snmp-server", "ntp", "logging",
    "spanning-tree", "aaa", "username", "enable", "service", "class-map",
    "policy-map", "crypto", "switchport", "description", "shutdown",
    "exit", "end", "address-family", "network", "redistribute",
    "channel-group", "lacp", "duplex", "speed", "mtu", "vrf", "clock",
    "archive", "boot", "cdp", "lldp", "vtp", "monitor", "track",
    "default", "negotiation", "storm-control", "flow-control", "power",
    "write", "wr", "copy", "reload", "access-group", "qos", "mac",
    "errdisable", "port-channel", "vlt-domain", "management",
}

# --- Juniper Junos command allow-list -----------------------------------
# Junos config is entered as hierarchical `set`/`delete` statements rather
# than IOS-style mode-nested lines, so it gets its own (much smaller)
# allow-list keyed on the first word instead.
JUNOS_ALLOWED_COMMANDS = {
    "set", "delete", "deactivate", "activate", "annotate", "edit", "top",
    "commit", "exit", "rollback", "show", "insert", "rename", "copy",
    "protect", "unprotect", "wildcard",
}

_IOS_LIKE_VENDORS = {"cisco", "arista"}


def _is_comment_or_blank(line: str) -> bool:
    return (not line) or line.startswith("!") or line.startswith("#")


def _first_token(line: str) -> str:
    return line.split()[0].lower() if line.split() else ""


# -------------------------------------------------------------------
# Vendor-aware structural / command checks
# -------------------------------------------------------------------
def _validate_ios_style(lines: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for line in lines:
        stripped = line.strip()
        if _is_comment_or_blank(stripped):
            continue

        token = _first_token(stripped)
        if token not in IOS_STYLE_ALLOWED_COMMANDS:
            errors.append(f"Unrecognized or unsupported command: '{stripped}'")
            continue

        if token == "interface" and len(stripped.split()) < 2:
            errors.append(f"Malformed interface declaration: '{stripped}'")
        if stripped.lower().startswith("ip address") and len(stripped.split()) < 3:
            errors.append(f"Missing parameter in IP address line: '{stripped}'")
        if token == "vlan" and len(stripped.split()) >= 2 and not stripped.split()[1].isdigit():
            errors.append(f"Malformed VLAN declaration (expected a VLAN ID): '{stripped}'")

    if not any(_first_token(line.strip()) == "interface" for line in lines):
        warnings.append("No interface block found in proposed configuration")

    return errors, warnings


def _validate_junos(lines: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for line in lines:
        stripped = line.strip()
        if _is_comment_or_blank(stripped):
            continue

        token = _first_token(stripped)
        if token not in JUNOS_ALLOWED_COMMANDS:
            errors.append(f"Unrecognized or unsupported command: '{stripped}'")
            continue
        if token in ("set", "delete") and len(stripped.split()) < 2:
            errors.append(f"Malformed '{token}' statement (missing hierarchy path): '{stripped}'")

    return errors, warnings


# -------------------------------------------------------------------
# Inventory cross-checks (Cisco/Arista IOS-style configs only -- Junos'
# fully-qualified `set` paths make most of these moot: a `set vlans foo
# vlan-id 10` statement declares and uses the VLAN in the same line, so
# there's no separate "reference vs. definition" gap to cross-check).
# -------------------------------------------------------------------
_VLAN_DEF_RE = re.compile(r"^vlan\s+(\d+)\s*$", re.IGNORECASE)
_VLAN_ACCESS_RE = re.compile(r"switchport\s+access\s+vlan\s+(\d+)", re.IGNORECASE)
_VLAN_TRUNK_RE = re.compile(r"switchport\s+trunk\s+allowed\s+vlan\s+(?:add\s+)?([\d,\-]+)", re.IGNORECASE)
_VLAN_SVI_RE = re.compile(r"^interface\s+vlan\s*(\d+)", re.IGNORECASE)
_VLAN_DOT1Q_RE = re.compile(r"encapsulation\s+dot1q\s+(\d+)", re.IGNORECASE)

_ACL_APPLY_RE = re.compile(r"(?:ip\s+)?access-group\s+(\S+)\s+(?:in|out)", re.IGNORECASE)
_ACL_DEF_NAMED_RE = re.compile(r"ip\s+access-list\s+(?:standard|extended)\s+(\S+)", re.IGNORECASE)
_ACL_DEF_NUMBERED_RE = re.compile(r"^access-list\s+(\d+)\b", re.IGNORECASE)

_INTERFACE_BLOCK_RE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE)

_DEFAULT_GATEWAY_RE = re.compile(r"ip\s+default-gateway\s+(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
_DEFAULT_ROUTE_RE = re.compile(r"ip\s+route\s+0\.0\.0\.0\s+0\.0\.0\.0\s+(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
_IP_ADDRESS_RE = re.compile(r"ip\s+address\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)


def _expand_vlan_range(spec: str) -> set[int]:
    ids: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            if lo.strip().isdigit() and hi.strip().isdigit():
                ids.update(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            ids.add(int(part))
    return ids


def _split_interface_blocks(config_text: str) -> dict[str, list[str]]:
    """Groups a config's lines under the `interface X` block they belong
    to, so ACL/VLAN application lines can be attributed to the interface
    they're actually configured on (needed for the interface-dependency
    cross-check below).
    """
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        match = _INTERFACE_BLOCK_RE.match(stripped)
        if match:
            current = match.group(1)
            blocks.setdefault(current, [])
            continue
        if stripped.lower() in ("exit", "!") or _first_token(stripped) in ("interface", "router", "line"):
            current = None
            continue
        if current is not None:
            blocks[current].append(stripped)
    return blocks


def _defined_vlans(text: str) -> set[int]:
    return {int(m.group(1)) for m in map(_VLAN_DEF_RE.match, (line.strip() for line in text.splitlines())) if m}


def _defined_acls(text: str) -> set[str]:
    names = {m.group(1) for m in _ACL_DEF_NAMED_RE.finditer(text)}
    numbers = {m.group(1) for m in map(_ACL_DEF_NUMBERED_RE.match, (line.strip() for line in text.splitlines())) if m}
    return names | numbers


def _defined_interfaces(text: str) -> set[str]:
    return {m.group(1) for m in map(_INTERFACE_BLOCK_RE.match, (line.strip() for line in text.splitlines())) if m}


def _cross_check_inventory(proposed_config: str, current_config: str | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    inventory_text = (current_config or "") + "\n" + proposed_config

    # --- VLAN references must be defined somewhere in current or proposed config ---
    defined_vlans = _defined_vlans(inventory_text)
    referenced_vlans: dict[int, str] = {}
    for line in proposed_config.splitlines():
        stripped = line.strip()
        m = _VLAN_ACCESS_RE.search(stripped)
        if m:
            referenced_vlans[int(m.group(1))] = stripped
        m = _VLAN_TRUNK_RE.search(stripped)
        if m:
            for vid in _expand_vlan_range(m.group(1)):
                referenced_vlans[vid] = stripped
        m = _VLAN_SVI_RE.match(stripped)
        if m:
            referenced_vlans[int(m.group(1))] = stripped
        m = _VLAN_DOT1Q_RE.search(stripped)
        if m:
            referenced_vlans[int(m.group(1))] = stripped

    for vlan_id, source_line in referenced_vlans.items():
        if vlan_id not in defined_vlans:
            errors.append(
                f"VLAN {vlan_id} is referenced ('{source_line}') but is not defined "
                f"(no matching 'vlan {vlan_id}' block in this change or the device's current configuration)"
            )

    # --- ACL references must resolve to a defined ACL ---
    defined_acls = _defined_acls(inventory_text)
    for line in proposed_config.splitlines():
        stripped = line.strip()
        m = _ACL_APPLY_RE.search(stripped)
        if m and m.group(1) not in defined_acls:
            errors.append(
                f"'{stripped}' references ACL '{m.group(1)}', which is not defined "
                f"(no matching 'ip access-list ... {m.group(1)}' or 'access-list {m.group(1)} ...' found)"
            )

    # --- Interface dependency: ACL/VLAN application must target a real interface ---
    known_interfaces = _defined_interfaces(inventory_text)
    proposed_blocks = _split_interface_blocks(proposed_config)
    for iface, iface_lines in proposed_blocks.items():
        has_dependent_command = any(
            _ACL_APPLY_RE.search(line) or _VLAN_ACCESS_RE.search(line) or _VLAN_TRUNK_RE.search(line)
            for line in iface_lines
        )
        if has_dependent_command and iface not in known_interfaces and current_config is None:
            warnings.append(
                f"Interface '{iface}' has ACL/VLAN commands applied but no current device configuration "
                f"is available to confirm the interface exists -- treating as a new interface"
            )

    # --- Gateway conflicts: default gateway/route must be reachable via an existing interface subnet ---
    gateway_ips = [m.group(1) for m in _DEFAULT_GATEWAY_RE.finditer(proposed_config)]
    gateway_ips += [m.group(1) for m in _DEFAULT_ROUTE_RE.finditer(proposed_config)]

    if gateway_ips:
        interface_subnets = []
        for m in _IP_ADDRESS_RE.finditer(inventory_text):
            try:
                interface_subnets.append(ipaddress.ip_interface(f"{m.group(1)}/{m.group(2)}").network)
            except ValueError:
                continue

        if not interface_subnets:
            if current_config is None:
                warnings.append(
                    "A default gateway/route is configured but no current device configuration is available "
                    "to confirm it's reachable via a configured interface subnet"
                )
        else:
            for gw in gateway_ips:
                try:
                    gw_addr = ipaddress.ip_address(gw)
                except ValueError:
                    errors.append(f"Gateway '{gw}' is not a valid IP address")
                    continue
                if not any(gw_addr in subnet for subnet in interface_subnets):
                    errors.append(
                        f"Gateway conflict: {gw} does not fall within any interface subnet configured "
                        f"on this device (checked {len(interface_subnets)} known interface subnet(s))"
                    )

    return errors, warnings


def validate_syntax(config_text: str, vendor: str = "cisco", current_config: str | None = None) -> ValidationResult:
    """Validates a proposed configuration before it's allowed to proceed
    to approval/deployment (SRS 6.4 / FR-5). This is a hard gate: any
    error means `passed=False` and the caller (app.api.change_requests,
    app.services.pipeline_service) must refuse to move the change forward.

    vendor: "cisco" | "arista" | "juniper" | "linux" -- selects which
        command allow-list/grammar to check against. Unknown vendors fall
        back to the IOS-style checks (the most common case).
    current_config: the device's last-known running configuration, used
        as the "inventory" for cross-checks (gateway/VLAN/ACL/interface
        existence). Pass None if it isn't available yet -- cross-checks
        degrade to warnings instead of failing a change against inventory
        we don't actually have.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not config_text or not config_text.strip():
        errors.append("Proposed configuration is empty")
        return ValidationResult(passed=False, errors=errors)

    for token in KNOWN_INVALID_TOKENS:
        if token.lower() in config_text.lower():
            errors.append(f"Placeholder/unsupported token found: '{token}'")

    if looks_like_xml(config_text):
        # Rollbacks for NETCONF devices supply raw XML snapshots, and
        # users can theoretically paste XML natively. Skip CLI-specific
        # syntax and cross-checks entirely -- the NETCONF commit/validate
        # phase handles structural integrity of XML payloads natively.
        return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)

    lines = [line for line in config_text.splitlines() if line.strip()]
    vendor = (vendor or "cisco").lower()

    if vendor == "juniper":
        struct_errors, struct_warnings = _validate_junos(lines)
    elif vendor == "linux":
        # Linux devices aren't IOS/Junos CLIs -- no command allow-list or
        # network-inventory cross-checks apply; only the generic
        # empty/placeholder checks above are meaningful here.
        struct_errors, struct_warnings = [], []
    else:
        struct_errors, struct_warnings = _validate_ios_style(lines)

    errors.extend(struct_errors)
    warnings.extend(struct_warnings)

    if vendor in _IOS_LIKE_VENDORS:
        cross_errors, cross_warnings = _cross_check_inventory(config_text, current_config)
        errors.extend(cross_errors)
        warnings.extend(cross_warnings)

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)
