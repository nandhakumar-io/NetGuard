"""First-hop-redundancy (HSRP/VRRP/GLBP) and PoE budget monitoring via SNMP.

Two independent, best-effort walkers in the same module/style as
snmp_service.walk_stp_edge_ports / walk_switchport_vlans -- neither
depends on a device being any particular vendor; each just returns {}
on any platform that doesn't implement the relevant MIB.

Why these exist
----------------
* FHRP: NetGuard previously had zero visibility into HSRP/VRRP/GLBP
  failover-group health. The two failure modes that matter operationally
  are exactly the two this module flags: a group with **no** active
  member (the standby silently died -- or was never there -- and nobody
  noticed until the "active" one failed too), and a group with **more
  than one** active member (split-brain -- both routers think they own
  the virtual IP, usually after an interface/track flap or a
  misconfigured priority).
* PoE: no budget visibility either. A switch feeding APs/phones/cameras
  can be sitting at 95%+ of its PSE power budget with zero indication in
  the UI -- the next AP you rack-and-stack simply won't power up, and
  the first symptom anyone sees is "why is this AP dark" rather than
  "the switch is out of PoE budget."

Both are polled live (no persisted history table yet -- see the
in-code note on poll_fhrp_and_poe below for what a follow-up migration
would add) and are meant to be called the same way
wireless_service.poll_wireless_controller is: per-device, best-effort,
never raising on a device that simply doesn't speak the relevant MIB.
"""
import logging

from app.services.snmp_service import (
    SnmpAuthConfig,
    _walk,
)

logger = logging.getLogger("netguard.fhrp_poe")

# ---------------------------------------------------------------------------
# HSRP -- CISCO-HSRP-MIB (Cisco-only; no vendor-neutral equivalent for HSRP
# specifically, unlike VRRP below)
# ---------------------------------------------------------------------------
# cHsrpGrpEntry (1.3.6.1.4.1.9.9.106.1.2.1.1), indexed by
# <ifIndex>.<idOrZero>.<groupNumber>
_OID_HSRP_STATE = "1.3.6.1.4.1.9.9.106.1.2.1.1.15"     # cHsrpGrpStandbyState
_OID_HSRP_VIP = "1.3.6.1.4.1.9.9.106.1.2.1.1.11"       # cHsrpGrpVirtualIpAddr
# cHsrpGrpStandbyState enum: 1=initial 2=learn 3=listen 4=speak 5=standby 6=active
_HSRP_STATE_NAMES = {
    "1": "initial", "2": "learn", "3": "listen", "4": "speak", "5": "standby", "6": "active",
}

# ---------------------------------------------------------------------------
# VRRP -- RFC 2787 VRRP-MIB (vendor-neutral; Juniper/Arista/most others)
# ---------------------------------------------------------------------------
# vrrpOperTable (1.3.6.1.2.1.68.1.3.1), indexed by <ifIndex>.<vrId>
_OID_VRRP_STATE = "1.3.6.1.2.1.68.1.3.1.4"    # vrrpOperState
_OID_VRRP_VIP = "1.3.6.1.2.1.68.1.3.1.9"      # vrrpOperVirtualIpAddress (first address)
# vrrpOperState enum: 1=initialize 2=backup 3=master
_VRRP_STATE_NAMES = {"1": "initialize", "2": "backup", "3": "master"}

# ---------------------------------------------------------------------------
# GLBP -- CISCO-GLBP-MIB (Cisco-only; group-level "who is AVG" state, not
# the per-AVF gateway-forwarder split, which is enough to detect "group has
# no active AVG" / split-brain the same way as HSRP/VRRP above)
# ---------------------------------------------------------------------------
# cglbpGroupEntry (1.3.6.1.4.1.9.9.315.1.2.1.1), indexed by <ifIndex>.<groupNumber>
_OID_GLBP_STATE = "1.3.6.1.4.1.9.9.315.1.2.1.1.7"     # cglbpGroupState
_OID_GLBP_VIP = "1.3.6.1.4.1.9.9.315.1.2.1.1.5"       # cglbpGroupVirtualIpAddr
# cglbpGroupState enum: 1=disabled 2=initial 3=listen 4=speak 5=active
_GLBP_STATE_NAMES = {"1": "disabled", "2": "initial", "3": "listen", "4": "speak", "5": "active"}


def _walk_fhrp_protocol(
    ip_address: str,
    auth: "SnmpAuthConfig",
    timeout: float,
    protocol: str,
    state_oid: str,
    vip_oid: str,
    state_names: dict,
    active_value: str,
) -> list[dict]:
    """Shared walk + normalize logic for HSRP/VRRP/GLBP -- all three MIBs
    have the same shape (one row per group, indexed by <ifIndex>.<...>.
    <groupNumber>, with a state enum column and a virtual-IP column), so
    this is one function parameterized per-protocol rather than three
    near-identical copies.

    Returns a list of {"protocol", "if_index", "group", "state",
    "virtual_ip", "is_active"} rows. Any SNMP failure (device doesn't
    implement this MIB at all -- the normal case for two of the three on
    any given device) yields [] rather than raising, same convention as
    walk_stp_edge_ports.
    """
    try:
        raw_state = _walk(ip_address, auth, state_oid, timeout)
        if not raw_state:
            return []
        raw_vip = _walk(ip_address, auth, vip_oid, timeout)
        rows = []
        for index, state_val in raw_state.items():
            parts = index.split(".")
            if len(parts) < 2:
                continue
            if_index, group = parts[0], parts[-1]
            state_code = str(state_val).strip()
            rows.append({
                "protocol": protocol,
                "if_index": if_index,
                "group": group,
                "state": state_names.get(state_code, f"unknown({state_code})"),
                "virtual_ip": raw_vip.get(index),
                "is_active": state_code == active_value,
            })
        return rows
    except Exception:
        logger.debug("fhrp: %s walk failed for %s", protocol, ip_address, exc_info=True)
        return []


def walk_hsrp_vrrp_state(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> list[dict]:
    """Walks all three FHRP protocols (HSRP, VRRP, GLBP) on a device and
    returns the combined, normalized group list. A device only ever
    implements at most one of these MIBs in practice (HSRP/GLBP are
    Cisco proprietary and mutually exclusive on the same interface;
    VRRP is what non-Cisco gear speaks instead), so this is safe to call
    unconditionally per-device without pre-checking vendor -- exactly
    the same "best-effort, empty walk = not applicable" convention as
    walk_switchport_vlans/walk_stp_edge_ports.
    """
    rows: list[dict] = []
    rows += _walk_fhrp_protocol(ip_address, auth, timeout, "hsrp", _OID_HSRP_STATE, _OID_HSRP_VIP, _HSRP_STATE_NAMES, active_value="6")
    rows += _walk_fhrp_protocol(ip_address, auth, timeout, "vrrp", _OID_VRRP_STATE, _OID_VRRP_VIP, _VRRP_STATE_NAMES, active_value="3")
    rows += _walk_fhrp_protocol(ip_address, auth, timeout, "glbp", _OID_GLBP_STATE, _OID_GLBP_VIP, _GLBP_STATE_NAMES, active_value="5")
    return rows


def find_fhrp_issues(fhrp_rows: list[dict]) -> list[dict]:
    """Groups the flat row list from walk_hsrp_vrrp_state by
    (protocol, if_index, group) -- the actual failover-group identity --
    and flags the two scenarios that matter operationally:

      * "no_active": every member of the group is in a non-active state
        (standby/listen/backup/etc, or the group has exactly one row and
        it isn't active). This is only meaningful once you can see *all*
        routers in the group, so this function is meant to be called
        against rows aggregated across every device in the group's
        subnet, not a single device's rows in isolation -- see
        poll_fhrp_and_poe's docstring for why persisting rows (rather
        than just alerting inline per-device) is the real fix.
      * "split_brain": more than one member of the group is active at
        the same time -- both think they own the virtual IP.

    Returns a list of {"protocol", "group", "virtual_ip", "issue",
    "member_count", "active_count"} dicts, one per problem group.
    """
    by_group: dict[tuple, list[dict]] = {}
    for row in fhrp_rows:
        key = (row["protocol"], row["group"], row.get("virtual_ip"))
        by_group.setdefault(key, []).append(row)

    issues = []
    for (protocol, group, vip), members in by_group.items():
        active_count = sum(1 for m in members if m["is_active"])
        if active_count == 0:
            issues.append({
                "protocol": protocol, "group": group, "virtual_ip": vip,
                "issue": "no_active", "member_count": len(members), "active_count": 0,
            })
        elif active_count > 1:
            issues.append({
                "protocol": protocol, "group": group, "virtual_ip": vip,
                "issue": "split_brain", "member_count": len(members), "active_count": active_count,
            })
    return issues


# ---------------------------------------------------------------------------
# PoE budget -- POWER-ETHERNET-MIB (RFC 3621, vendor-neutral)
# ---------------------------------------------------------------------------
# pethMainPseEntry (1.3.6.1.2.1.105.1.3.1.1), one row per PSE (power
# sourcing equipment) unit -- usually one per switch, occasionally more
# on a stack/chassis with multiple PSE controllers.
_OID_PSE_USAGE_THRESHOLD = "1.3.6.1.2.1.105.1.3.1.1.2"  # pethMainPseUsageThreshold (percent, 1-99)
_OID_PSE_POWER = "1.3.6.1.2.1.105.1.3.1.1.3"            # pethMainPseConsumptionPower (mW currently drawn)
# pethPsePortEntry (1.3.6.1.2.1.105.1.4.1.1) -- one row per PoE-capable
# switchport, indexed by <groupIndex>.<portIndex>.
_OID_PORT_DETECTION = "1.3.6.1.2.1.105.1.4.1.1.5"  # pethPsePortDetectionStatus (3=deliveringPower)
_OID_PORT_POWER = "1.3.6.1.2.1.105.1.4.1.1.7"      # pethPsePortPowerAllocated on some agents / actual draw on others -- see poll below
_PSE_DELIVERING_POWER = "3"


def walk_poe_status(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict | None:
    """POWER-ETHERNET-MIB budget summary for a switch's PSE. Returns
    None (rather than a zeroed-out dict) if the switch has no PoE
    hardware at all -- pethMainPseEntry walks empty on any non-PoE
    switch, and reporting "0% used" there would be actively misleading
    (there's no budget to be near, not a healthy 0%).

    ``consumption_percent`` is computed from the standard vendor-neutral
    columns (consumption / (100 threshold-normalized capacity)) rather
    than trusted from a single vendor OID for max capacity, since
    RFC 3621 doesn't expose a "max budget in watts" scalar directly --
    pethMainPseUsageThreshold is a percent-of-capacity *alarm* threshold,
    not the capacity itself, so this reports raw consumption (mW) and
    the configured alarm threshold, and lets the caller (alert rule /
    UI) decide what "near budget" means rather than guessing a wattage
    ceiling this MIB doesn't actually provide.
    """
    try:
        power = _walk(ip_address, auth, _OID_PSE_POWER, timeout)
        if not power:
            return None
        threshold = _walk(ip_address, auth, _OID_PSE_USAGE_THRESHOLD, timeout)
        detection = _walk(ip_address, auth, _OID_PORT_DETECTION, timeout)
        powered_ports = sum(1 for v in detection.values() if str(v).strip() == _PSE_DELIVERING_POWER)
        pse_rows = []
        for pse_index, mw in power.items():
            try:
                mw_int = int(str(mw).strip())
            except (ValueError, TypeError):
                continue
            pse_rows.append({
                "pse_index": pse_index,
                "consumption_mw": mw_int,
                "usage_threshold_percent": _safe_int(threshold.get(pse_index)),
            })
        if not pse_rows:
            return None
        return {"pses": pse_rows, "powered_port_count": powered_ports}
    except Exception:
        logger.debug("poe: walk failed for %s", ip_address, exc_info=True)
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Combined per-device poll, mirroring wireless_service.poll_wireless_controller
# ---------------------------------------------------------------------------
def poll_fhrp_and_poe(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Single entry point for a scheduled/on-demand poll of one device:
    walks FHRP groups + PoE budget and returns both, plus any FHRP
    issues visible from *this device's* rows alone (a true split-brain
    check needs every group member's row aggregated across devices --
    see find_fhrp_issues's docstring -- so `issues` here is a
    single-device best-effort view: "no_active" can be detected reliably
    from one device if it's the only member configured on it, but
    cross-device aggregation is a follow-up once these rows are
    persisted).

    NOTE: this intentionally does not write to the DB yet. Given the
    size of adding a fully persisted table (mirroring wireless_aps/
    wireless_ssids: model + migration + schema + CRUD API + a Celery
    poll task wired into the existing scheduler) alongside PoE, this
    first pass keeps both as on-demand/live SNMP reads exposed through
    a thin API layer, matching how e.g. `POST /wireless/aps/{id}/check`
    does a live reachability check without persisting history. A
    follow-up migration (persisted fhrp_groups / poe_status tables +
    alert_rule_engine hooks, same shape as evaluate_ap_rules) is the
    natural next step once this is validated against real hardware --
    flagging that scope explicitly rather than silently shipping a
    partial persisted schema.
    """
    fhrp_rows = walk_hsrp_vrrp_state(ip_address, auth, timeout)
    poe = walk_poe_status(ip_address, auth, timeout)
    return {
        "fhrp_groups": fhrp_rows,
        "fhrp_issues": find_fhrp_issues(fhrp_rows),
        "poe": poe,
    }
