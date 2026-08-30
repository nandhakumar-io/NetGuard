"""Wireless AP / SSID monitoring via SNMP.

Polls a Cisco AireOS WLC (or compatible SNMP-enabled wireless controller)
using AIRESPACE-WIRELESS-MIB OIDs and upserts the results into
wireless_aps / wireless_ssids (see migration 0104 and app.models.wireless).

Design notes
------------
* Best-effort: if a device isn't a WLC (walk returns empty) the function
  returns an empty snapshot without error -- callers don't need to filter
  devices before calling.
* Uses the _walk/_get_first_table_value helpers already in snmp_service
  rather than duplicating SNMP session setup.
* Per-band client split: AireOS SNMP doesn't expose this directly in a
  simple scalar, so it's derived by walking bsnMobileStationAPMacAddr
  (which AP each associated client is on) + bsnMobileStationPhyType
  (which radio/band), joined back to bsnAPTable via bsnAPDot3MacAddress.
  Bounded by _MAX_CLIENTS_WALKED and fully best-effort: any failure here
  leaves band_2g_clients / band_5g_clients as None ("n/a" in the UI)
  without affecting the rest of the poll.
"""
import datetime
import logging
import re
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.wireless import WirelessAP
    from app.services.snmp_service import SnmpAuthConfig

logger = logging.getLogger("netguard.wireless")

# ---------------------------------------------------------------------------
# Cisco AireOS AIRESPACE-WIRELESS-MIB OIDs
# ---------------------------------------------------------------------------
# bsnAPTable (1.3.6.1.4.1.14179.2.2.1.1) -- one row per AP
_OID_AP_NAME = "1.3.6.1.4.1.14179.2.2.1.1.3"       # bsnAPName
_OID_AP_OPER = "1.3.6.1.4.1.14179.2.2.1.1.6"       # bsnAPOperationStatus (1=associated)
_OID_AP_MODEL = "1.3.6.1.4.1.14179.2.2.1.1.16"     # bsnAPModel
_OID_AP_IP = "1.3.6.1.4.1.14179.2.2.1.1.19"        # bsnApIpAddress
_OID_AP_CLIENTS = "1.3.6.1.4.1.14179.2.2.1.1.38"   # bsnApNumOfUsers
_OID_AP_MAC = "1.3.6.1.4.1.14179.2.2.1.1.2"        # bsnAPDot3MacAddress -- joins to bsnMobileStationAPMacAddr below

# Extra bsnAPTable columns for the troubleshooting fields added to the
# Wireless page (uptime / software version / serial). NOTE: unlike the
# core columns above (name/oper/model/ip/clients, which are stable
# across AireOS releases and widely field-verified), these three column
# indices vary more between AireOS/IOS-XE WLC firmware trains. Treat
# them as a starting point: if they come back empty on your controller,
# `snmpwalk -v2c -c <community> <wlc-ip> 1.3.6.1.4.1.14179.2.2.1.1` and
# match the OIDs actually returned against AIRESPACE-WIRELESS-MIB for
# your firmware version, then update these three constants -- nothing
# else in this module needs to change.
_OID_AP_UPTIME = "1.3.6.1.4.1.14179.2.2.1.1.31"     # bsnAPUpTime
_OID_AP_SW_VERSION = "1.3.6.1.4.1.14179.2.2.1.1.10"  # bsnAPSoftwareVersion
_OID_AP_SERIAL = "1.3.6.1.4.1.14179.2.2.1.1.43"      # bsnAPSerialNumber

# bsnAPIfTable (1.3.6.1.4.1.14179.2.2.2.1) -- one row per *radio* on an
# AP (index is "<ap_index>.<slot>", slot 0 = 802.11b/g/n = 2.4GHz,
# slot 1 = 802.11a/n/ac = 5GHz on the vast majority of dual-radio APs).
# Same "verify against your controller" caveat as above applies to the
# exact column numbers.
_OID_RADIO_CHANNEL = "1.3.6.1.4.1.14179.2.2.2.1.4"     # bsnAPIfPhyChannelNumber
_OID_RADIO_TX_POWER = "1.3.6.1.4.1.14179.2.2.2.1.6"    # bsnAPIfPhyTxPowerLevel (controller power index, not dBm)
_OID_RADIO_NOISE = "1.3.6.1.4.1.14179.2.2.2.1.14"      # bsnAPIfNoiseNow (dBm)
_OID_RADIO_UTIL = "1.3.6.1.4.1.14179.2.2.2.1.11"       # bsnAPIfLoadChannelUtilization (percent)
_RADIO_SLOT_BAND = {"0": "2g", "1": "5g"}

# bsnMobileStationTable (1.3.6.1.4.1.14179.2.1.4.1) -- one row per
# *associated client*, used only for the per-band (2.4/5GHz) split on
# top of bsnApNumOfUsers above (which is band-agnostic). Walking every
# client on the controller is a heavier ask than the AP/SSID tables
# (a busy campus WLC can have thousands of stations), so this is capped
# by _MAX_CLIENTS_WALKED below and, like the rest of this module, is
# best-effort: any failure here just leaves the band split as None
# (shown as "n/a"), never fails the AP/SSID poll it's layered on top of.
_OID_STA_AP_MAC = "1.3.6.1.4.1.14179.2.1.4.1.4"     # bsnMobileStationAPMacAddr -- which AP this client is on
_OID_STA_PHY_TYPE = "1.3.6.1.4.1.14179.2.1.4.1.25"  # bsnMobileStationPhyType -- which radio/band it's on
_MAX_CLIENTS_WALKED = 4000
# bsnMobileStationPhyType enum -> band. dot11b/g and the 2.4GHz flavors
# of 11n/11ax map to 2.4GHz; dot11a and the 5GHz flavors of 11n/11ac/11ax
# map to 5GHz. Unrecognized/future values are dropped from the split
# rather than guessed.
_PHY_TYPE_BAND = {
    "1": "2g", "3": "2g", "4": "2g", "7": "2g", "8": "2g",   # dot11b, dot11g, dot11g-only, dot11n-2.4, dot11ax-2.4
    "2": "5g", "5": "5g", "6": "5g", "9": "5g",              # dot11a, dot11n-5, dot11ac, dot11ax-5
}

# bsnDot11EssTable (1.3.6.1.4.1.14179.2.1.2.1) -- one row per SSID profile
_OID_ESS_SSID = "1.3.6.1.4.1.14179.2.1.2.1.1"       # bsnDot11EssSsid
_OID_ESS_ADMIN = "1.3.6.1.4.1.14179.2.1.2.1.2"      # bsnDot11EssAdminStatus
_OID_ESS_CLIENTS = "1.3.6.1.4.1.14179.2.1.2.1.38"   # bsnDot11EssNumberOfMobileStations

# Security-mode columns, same table as the SSID name/admin/client columns
# above -- added so an open/WEP/WPA1-TKIP SSID shows up as a "Weak SSID
# Security" finding instead of going unnoticed next to the existing
# rogue-AP detection. Same caveat as the AP uptime/sw-version/serial
# columns further up: these three column indices are less
# universally-stable across AireOS/IOS-XE WLC firmware trains than the
# core name/admin/client columns, so if they come back empty on your
# controller, `snmpwalk` bsnDot11EssTable and match against
# AIRESPACE-WIRELESS-MIB for your firmware, then update these three
# constants -- nothing else in this module needs to change.
_OID_ESS_WEP_STATE = "1.3.6.1.4.1.14179.2.1.2.1.40"    # bsnDot11EssWepState (1=enabled, 2=disabled)
_OID_ESS_WPA1_ENABLE = "1.3.6.1.4.1.14179.2.1.2.1.53"  # bsnDot11EssWPA1Enable (1=enabled)
_OID_ESS_WPA2_ENABLE = "1.3.6.1.4.1.14179.2.1.2.1.60"  # bsnDot11EssWPA2Enable (1=enabled)

# ---------------------------------------------------------------------------
# MikroTik RouterOS MIKROTIK-MIB (1.3.6.1.4.1.14988) -- standalone AP client
# count + SSID, for manually-added APs with no WLC to poll (see
# poll_standalone_ap below). Verified against MikroTik's own MIB /
# changelog and community-confirmed snmpwalk output, NOT guessed:
#   - mtxrWlApClientCount: MikroTik forum "New SNMP MIB file for MikroTik
#     RouterOS" (the count OID's 2010 rename) + Observium's MIB browser
#     entry (Indexes: mtxrWlApIndex, Syntax: Counter32).
#   - mtxrWlApSsid: RouterOS `/interface wireless print oid` output
#     (community-posted), same table/index as client count.
# ROS7's newer wifi driver (not wifi-qcom-ac / legacy wireless package)
# does NOT populate this table on some builds -- confirmed by a MikroTik
# forum thread showing "No Such Object" on ROS7 for one of three test
# APs. poll_standalone_ap treats an empty walk as "not supported on this
# firmware" rather than silently reporting 0, same as the WLC poller
# already does for a non-WLC device.
_OID_MTXR_WLAP_SSID = "1.3.6.1.4.1.14988.1.1.1.3.1.4"          # mtxrWlApSsid, indexed by mtxrWlApIndex
_OID_MTXR_WLAP_CLIENT_COUNT = "1.3.6.1.4.1.14988.1.1.1.3.1.6"  # mtxrWlApClientCount, same index

# ---------------------------------------------------------------------------
# Ruckus WLAN MIB OIDs (RUCKUS-WLAN-MIB, enterprise 1.3.6.1.4.1.25053)
#
# Sourced from RUCKUS-WLAN-MIB published by CommScope (open, downloadable
# from support.ruckuswireless.com) and confirmed against community snmpwalk
# output for Unleashed/ZoneDirector. ruckusZDApNumSta returns the current
# client count indexed by AP MAC as an OID suffix, ruckusZDWlanSsid gives
# the SSID name indexed by WLAN index. The AP-to-SSID mapping is one-to-
# many (each AP broadcasts all configured WLANs), so SSID names come from
# the global WLAN table rather than per-AP rows.
# NOTE: These OIDs apply to standalone Unleashed APs and Zone Director-
# managed deployments polled through the ZD. SmartZone uses a different
# REST API and doesn't expose the same SNMP MIB -- if your AP is only
# accessible through SmartZone and has no standalone-mode SNMP agent, this
# poll will return an empty walk (treated as "not supported" same as the
# MikroTik empty-walk path above).
_OID_RUCKUS_AP_NUM_STA = "1.3.6.1.4.1.25053.1.2.2.1.1.1.2.1.4"  # ruckusZDApNumSta (client count per AP MAC)
_OID_RUCKUS_WLAN_SSID = "1.3.6.1.4.1.25053.1.2.2.1.1.3.2.1.2"  # ruckusZDWlanSsid (SSID name, per WLAN index)
# Fallback: IEEE 802.11 MIB dot11StationCount (vendor-neutral, widely
# supported including on Ruckus Unleashed firmware; returns total client
# count for the AP as a single scalar-like table). Used when the
# Ruckus-enterprise walk returns empty (e.g. older firmware or limited
# community string access).
_OID_DOT11_STATION_COUNT = "1.3.6.1.2.1.26.1.1.1.7"  # dot11StationCount, indexed by ifIndex

# ---------------------------------------------------------------------------
# TP-Link Omada/EAP SNMP OIDs (TPLINK enterprise 1.3.6.1.4.1.11863)
#
# TP-Link EAP series (EAP225, EAP245, EAP670, EAP660 HD, etc.) expose
# per-radio client counts and SSID names under the tplinkAp WLAN MIB when
# set up in standalone mode or as part of an Omada SDN controller deployment
# that allows AP-level SNMP queries. OIDs sourced from TP-Link's published
# TPLINK-WLAN-MIB and field-confirmed snmpwalk output; the controller-level
# MIB (via the Omada SDN Controller software SNMP interface) uses different
# subtrees and is NOT covered here -- this is for direct-AP polling only.
# Per-AP (standalone): tpApStationNumber gives current client count per radio
# (indexed by radio index), tpApSsid gives the configured SSID name per
# radio. A single AP with 2 radios returns 2 rows; sum across all rows
# for the total client count, concatenate distinct SSID names.
_OID_TPLINK_AP_STATION_NUMBER = "1.3.6.1.4.1.11863.6.4.1.2.1.1.10"  # tpApStationNumber (clients per radio)
_OID_TPLINK_AP_SSID = "1.3.6.1.4.1.11863.6.4.1.2.1.1.2"           # tpApSsid (SSID name per radio)

_SNMP_TRUE = "1"


def _classify_ssid_security(wep: str | None, wpa1: str | None, wpa2: str | None) -> tuple[str, bool]:
    """Derives a human-readable security_mode label + is_weak_security
    flag from the three raw bsnDot11Ess* booleans above.

    Priority: WPA2 present -> "WPA2" (not weak, regardless of whether
    WPA1 is *also* enabled for backwards compatibility -- that's a
    normal mixed-mode config, not a vulnerability by itself). Otherwise
    WPA1-only -> "WPA/TKIP" (weak: TKIP is deprecated and WPA1 has no
    AES requirement). Otherwise WEP -> "WEP" (weak). Otherwise none of
    the three enabled -> "Open" (weak: unencrypted). Missing/unreadable
    OIDs (all three None, i.e. this controller's firmware doesn't
    expose these columns at the indices above) fall through to "Unknown"
    with is_weak_security=None-equivalent (False, so it doesn't
    false-positive a finding this app can't actually confirm) --
    callers should treat "Unknown" as "needs the OID indices checked",
    not as "this SSID is secure".
    """
    if wep is None and wpa1 is None and wpa2 is None:
        return "Unknown", False
    if wpa2 == _SNMP_TRUE:
        return "WPA2", False
    if wpa1 == _SNMP_TRUE:
        return "WPA/TKIP", True
    if wep == _SNMP_TRUE:
        return "WEP", True
    return "Open", True


def _safe_int(val: str | None) -> int | None:
    """Parse an SNMP integer string tolerantly; return None on failure."""
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def normalize_mac(raw: str | None) -> str | None:
    """Normalizes a MAC address to lowercase colon-separated form
    (aa:bb:cc:dd:ee:ff) regardless of how the source formatted it --
    SNMP agents/pysnmp report chassis IDs and bsnAPDot3MacAddress in a
    mix of "AA:BB:CC:DD:EE:FF", "AA-BB-CC-DD-EE-FF", and bare
    "AABBCCDDEEFF" depending on device/library, and a straight string
    match between WirelessAP.mac_address (from bsnAPDot3MacAddress) and
    DiscoveredNeighbor.neighbor_chassis_id (from lldpRemChassisId) would
    silently correlate nothing if the two ever used different casing/
    separators. Returns None (rather than a garbage value) for anything
    that isn't 12 hex digits once separators are stripped.
    """
    if not raw:
        return None
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", str(raw))
    if len(hex_only) != 12:
        return None
    hex_only = hex_only.lower()
    return ":".join(hex_only[i:i + 2] for i in range(0, 12, 2))


# Vendor strings that show up in an AP's LLDP sysDescr, used by
# find_unregistered_aps below to flag "this switchport looks like it has
# an AP on it" without depending on any single vendor's proprietary
# rogue-AP MIB (AireOS's bsnTrapsRogueApTable, the closest built-in
# equivalent, only ever fires for a Cisco WLC-managed environment -- no
# help at all for a Ruckus/TP-Link/Omada fleet, which most sites
# actually run). Deliberately broad/lowercase; matched case-insensitively
# against the whole sysDescr string.
_AP_SYSDESCR_MARKERS = [
    "ruckus", "tp-link", "tplink", "eap", "omada", "ubiquiti", "unifi",
    "aruba", "instant ap", "aironet", "meraki mr", "access point",
    "wireless access point",
]


def find_switchport_for_ap(db: Session, ap) -> dict | None:
    """Best-effort "which switchport is this AP plugged into" lookup for
    a single WirelessAP, by MAC address against the most recent LLDP
    neighbor rows discovered from any managed switch (see
    DiscoveredNeighbor.neighbor_chassis_id).

    Returns {"device_id", "hostname", "port"} for the first match, or
    None if the AP has no usable MAC or nothing in the LLDP neighbor
    table matches it (AP not directly LLDP-adjacent to a managed switch,
    LLDP not enabled on the AP, or a switch that hasn't been discovered
    recently).
    """
    from app.models.device import Device
    from app.models.discovered_neighbor import DiscoveredNeighbor

    mac = normalize_mac(ap.mac_address)
    if not mac:
        return None
    row = (
        db.query(DiscoveredNeighbor, Device)
        .join(Device, Device.id == DiscoveredNeighbor.device_id)
        .filter(DiscoveredNeighbor.neighbor_chassis_id.isnot(None))
        .all()
    )
    for neighbor, switch in row:
        if normalize_mac(neighbor.neighbor_chassis_id) == mac:
            return {"device_id": str(switch.id), "hostname": switch.hostname, "port": neighbor.local_port}
    return None


def find_switchports_for_aps(db: Session, aps: list) -> dict[str, dict]:
    """Bulk version of find_switchport_for_ap for a list page (the
    Wireless page's AP grid) -- one pass over DiscoveredNeighbor instead
    of one query per AP card. Returns {ap.id (str): {device_id,
    hostname, port}} for every AP that has a match.
    """
    from app.models.device import Device
    from app.models.discovered_neighbor import DiscoveredNeighbor

    mac_to_ap_id = {}
    for ap in aps:
        mac = normalize_mac(ap.mac_address)
        if mac:
            mac_to_ap_id[mac] = str(ap.id)
    if not mac_to_ap_id:
        return {}

    result: dict[str, dict] = {}
    rows = (
        db.query(DiscoveredNeighbor, Device)
        .join(Device, Device.id == DiscoveredNeighbor.device_id)
        .filter(DiscoveredNeighbor.neighbor_chassis_id.isnot(None))
        .all()
    )
    for neighbor, switch in rows:
        mac = normalize_mac(neighbor.neighbor_chassis_id)
        ap_id = mac_to_ap_id.get(mac) if mac else None
        if ap_id and ap_id not in result:
            result[ap_id] = {"device_id": str(switch.id), "hostname": switch.hostname, "port": neighbor.local_port}
    return result


def find_unregistered_aps(db: Session) -> list[dict]:
    """Flags switchports whose LLDP neighbor looks like an access point
    (sysDescr matches a known AP vendor string, see
    _AP_SYSDESCR_MARKERS) but whose MAC doesn't match any AP already
    tracked on the Wireless page -- i.e. "there's a physical AP plugged
    in here that NetGuard doesn't know about".

    This is the practical, vendor-agnostic substitute for Cisco AireOS's
    bsnTrapsRogueApTable: it works for any LLDP-capable AP regardless of
    controller (or lack of one), which matters for a mostly-Ruckus/
    TP-Link fleet where there's often no WLC to poll rogue-AP traps from
    in the first place. It's a weaker signal than a real WIDS/rogue-AP
    feature (it can't see APs that don't speak LLDP, and it flags
    "unregistered", not "actively hostile") -- good enough to catch an
    employee's personal travel router or an AP that fell out of
    inventory, not a substitute for real wireless intrusion detection.

    Returns one dict per unmatched AP-like neighbor: name, sys_desc,
    switch hostname/device_id, and port.
    """
    from app.models.device import Device
    from app.models.discovered_neighbor import DiscoveredNeighbor
    from app.models.wireless import WirelessAP

    known_macs = {
        normalize_mac(mac)
        for (mac,) in db.query(WirelessAP.mac_address).filter(WirelessAP.mac_address.isnot(None)).all()
        if normalize_mac(mac)
    }

    rows = (
        db.query(DiscoveredNeighbor, Device)
        .join(Device, Device.id == DiscoveredNeighbor.device_id)
        .filter(DiscoveredNeighbor.neighbor_sys_desc.isnot(None))
        .all()
    )
    findings = []
    seen_macs: set[str] = set()
    for neighbor, switch in rows:
        sys_desc = (neighbor.neighbor_sys_desc or "").lower()
        if not any(marker in sys_desc for marker in _AP_SYSDESCR_MARKERS):
            continue
        mac = normalize_mac(neighbor.neighbor_chassis_id)
        if mac and mac in known_macs:
            continue  # already a tracked AP, not "unregistered"
        if mac and mac in seen_macs:
            continue  # same AP seen from more than one switch/port -- report once
        if mac:
            seen_macs.add(mac)
        findings.append({
            "neighbor_name": neighbor.neighbor_name,
            "mac_address": mac,
            "sys_desc": neighbor.neighbor_sys_desc,
            "switch_device_id": str(switch.id),
            "switch_hostname": switch.hostname,
            "port": neighbor.local_port,
            "discovered_at": neighbor.discovered_at,
        })
    return findings


def poll_standalone_ap(db: Session, ap) -> dict:
    """SNMP client-count + SSID refresh for a manually-added AP with no
    WLC to poll (see WirelessAP.controller_device_id's docstring) --
    the gap that used to leave manual APs stuck at whatever client_count
    was typed in at creation time forever, since /aps/{id}/check only
    ever pinged the AP, never re-read anything from it.

    Prefers the AP's own SNMP credentials (WirelessAP.snmp_* columns --
    see build_snmp_auth_from_ap) when configured. Falls back to reusing
    an existing managed Device's SNMP config at the same IP for AP rows
    that predate this (or that were never given their own creds), so
    nothing that already worked breaks.

    Supported vendors: "mikrotik", "ruckus", "tplink". Every other vendor
    returns an explicit "not yet supported" result instead of a silently-
    wrong number. Extending this to another vendor means adding a
    real, source-cited OID block here (see the MikroTik, Ruckus, and
    TP-Link constant blocks above), not assuming one vendor's table
    layout applies to another.
    """
    from app.models.device import Device

    ip = ap.management_ip or ap.ap_ip_address
    if not ip:
        return {"error": "no_ip", "message": "AP has no management IP or AP IP address to poll."}

    auth = build_snmp_auth_from_ap(ap)
    device = None
    if auth is None:
        device = db.query(Device).filter(Device.ip_address == ip).first()
        if device is None:
            return {
                "error": "no_snmp_credentials",
                "message": "This AP has no SNMP credentials of its own -- set them via "
                "PATCH /wireless/aps/{id}/snmp-credentials, or add this IP as a managed "
                "Device with SNMP configured.",
            }
        auth = build_snmp_auth(db, device)
        if auth is None:
            return {
                "error": "snmp_not_configured",
                "message": f"Device {device.hostname} matches this AP's IP but doesn't have SNMP configured/enabled.",
            }

    vendor = (ap.vendor or "").lower()
    timeout = 5.0

    if vendor == "mikrotik":
        return _poll_standalone_mikrotik(db, ap, device, auth, timeout)
    if vendor == "ruckus":
        return _poll_standalone_ruckus(db, ap, device, auth, timeout)
    if vendor == "tplink":
        return _poll_standalone_tplink(db, ap, device, auth, timeout)

    return {
        "error": "vendor_not_supported",
        "message": f"Standalone SNMP polling isn't implemented yet for vendor '{vendor}' -- "
        "supported vendors: mikrotik, ruckus, tplink. Tell NetGuard's maintainer which vendor this AP "
        "actually is and its client-count/SSID OIDs can be added the same way these were.",
    }


def _upsert_ssids_for_standalone(
    db,
    device,
    ap,
    ssids: dict[str, str],
    client_counts: dict[str, int | None],
    now,
) -> int:
    """Shared SSID upsert logic for all standalone AP vendor paths.
    `ssids` maps index -> ssid_name; `client_counts` maps the same
    index -> client count (or None if count unavailable for that slot).
    Returns the count of SSID rows actually upserted.
    """
    from app.models.wireless import WirelessSSID

    ssid_count = 0
    for idx, ssid_name in ssids.items():
        if not ssid_name:
            continue
        existing = (
            db.query(WirelessSSID)
            .filter(WirelessSSID.controller_device_id == device.id, WirelessSSID.ssid_index == idx)
            .first()
        )
        if existing is None:
            existing = WirelessSSID(controller_device_id=device.id, ssid_index=idx, ssid_name=ssid_name)
            db.add(existing)
        existing.ssid_name = ssid_name
        existing.mobile_station_count = client_counts.get(idx)
        existing.admin_status = 1  # an entry in the walk table is active by definition
        existing.polled_at = now
        ssid_count += 1
    return ssid_count


def _poll_standalone_mikrotik(db, ap, device, auth, timeout: float) -> dict:
    """MikroTik RouterOS WLAN client-count + SSID poll using MIKROTIK-MIB.
    See the _OID_MTXR_* constant block above for OID sources.
    """
    from app.services.snmp_service import _walk

    ip = device.ip_address
    client_counts = _walk(ip, auth, _OID_MTXR_WLAP_CLIENT_COUNT, timeout)
    ssids = _walk(ip, auth, _OID_MTXR_WLAP_SSID, timeout)
    if not client_counts and not ssids:
        return {
            "error": "empty_walk",
            "message": "AP answered SNMP but returned no wireless-interface table rows -- either it's "
            "running RouterOS 7's newer wifi driver (which doesn't expose this table on some builds) "
            "or SNMP is reachable but the wireless package isn't active on this device.",
        }

    total_clients = sum(_safe_int(v) or 0 for v in client_counts.values())
    now = datetime.datetime.now(datetime.timezone.utc)
    ap.client_count = total_clients
    ap.polled_at = now

    count_by_idx = {idx: _safe_int(v) for idx, v in client_counts.items()}
    ssid_count = _upsert_ssids_for_standalone(db, device, ap, ssids, count_by_idx, now)
    db.commit()
    db.refresh(ap)
    return {"client_count": total_clients, "ssid_count": ssid_count}


def _poll_standalone_ruckus(db, ap, device, auth, timeout: float) -> dict:
    """Ruckus Unleashed / ZoneDirector client-count + SSID poll using
    RUCKUS-WLAN-MIB. See the _OID_RUCKUS_* constant block above for OID
    sources and caveats (SmartZone-only deployments won't populate this).
    """
    from app.services.snmp_service import _walk

    ip = device.ip_address
    # Primary: Ruckus enterprise MIB -- ruckusZDApNumSta gives client count
    # per AP MAC, ruckusZDWlanSsid gives SSID names per WLAN index.
    sta_rows = _walk(ip, auth, _OID_RUCKUS_AP_NUM_STA, timeout)
    ssid_rows = _walk(ip, auth, _OID_RUCKUS_WLAN_SSID, timeout)

    # Fallback: IEEE 802.11 MIB dot11StationCount if Ruckus enterprise
    # returned nothing (older firmware, limited SNMP community access, or
    # SmartZone-managed AP with no local SNMP agent).
    if not sta_rows:
        dot11_rows = _walk(ip, auth, _OID_DOT11_STATION_COUNT, timeout)
        if dot11_rows:
            total_clients = sum(_safe_int(v) or 0 for v in dot11_rows.values())
            now = datetime.datetime.now(datetime.timezone.utc)
            ap.client_count = total_clients
            ap.polled_at = now
            db.commit()
            db.refresh(ap)
            return {"client_count": total_clients, "ssid_count": 0, "note": "client count via IEEE 802.11 MIB fallback"}
        return {
            "error": "empty_walk",
            "message": "Ruckus AP answered SNMP but returned no wireless-client rows (tried Ruckus "
            "WLAN MIB ruckusZDApNumSta and IEEE 802.11 dot11StationCount). This AP may be "
            "SmartZone-managed (SmartZone-only deployments don't expose this MIB locally) "
            "or may require a different SNMP community for the wireless subtree.",
        }

    total_clients = sum(_safe_int(v) or 0 for v in sta_rows.values())
    now = datetime.datetime.now(datetime.timezone.utc)
    ap.client_count = total_clients
    ap.polled_at = now

    # ruckusZDWlanSsid gives global SSID names -- no per-SSID client count
    # in this table, so all client counts are None for the SSID rows.
    ssid_count = _upsert_ssids_for_standalone(db, device, ap, ssid_rows, {}, now)
    db.commit()
    db.refresh(ap)
    return {"client_count": total_clients, "ssid_count": ssid_count}


def _poll_standalone_tplink(db, ap, device, auth, timeout: float) -> dict:
    """TP-Link EAP/Omada standalone AP client-count + SSID poll using
    TPLINK WLAN MIB. See the _OID_TPLINK_* constant block above for OID
    sources. Each radio is one table row; sums across all radios for the
    total client count, deduplicates SSID names (dual-band APs broadcast
    the same SSID on both radios).
    """
    from app.services.snmp_service import _walk

    ip = device.ip_address
    # Primary: TP-Link enterprise MIB -- tpApStationNumber (clients per
    # radio) and tpApSsid (SSID name per radio).
    client_rows = _walk(ip, auth, _OID_TPLINK_AP_STATION_NUMBER, timeout)
    ssid_rows = _walk(ip, auth, _OID_TPLINK_AP_SSID, timeout)

    # Fallback: IEEE 802.11 MIB dot11StationCount (works on many TP-Link
    # EAP models even when the enterprise MIB isn't enabled).
    if not client_rows and not ssid_rows:
        dot11_rows = _walk(ip, auth, _OID_DOT11_STATION_COUNT, timeout)
        if dot11_rows:
            total_clients = sum(_safe_int(v) or 0 for v in dot11_rows.values())
            now = datetime.datetime.now(datetime.timezone.utc)
            ap.client_count = total_clients
            ap.polled_at = now
            db.commit()
            db.refresh(ap)
            return {"client_count": total_clients, "ssid_count": 0, "note": "client count via IEEE 802.11 MIB fallback"}
        return {
            "error": "empty_walk",
            "message": "TP-Link AP answered SNMP but returned no wireless rows (tried TP-Link WLAN "
            "MIB tpApStationNumber and IEEE 802.11 dot11StationCount). Ensure SNMP is enabled "
            "on the EAP and the community string gives access to the 1.3.6.1.4.1.11863 subtree.",
        }

    total_clients = sum(_safe_int(v) or 0 for v in client_rows.values())
    now = datetime.datetime.now(datetime.timezone.utc)
    ap.client_count = total_clients
    ap.polled_at = now

    count_by_idx = {idx: _safe_int(v) for idx, v in client_rows.items()}
    ssid_count = _upsert_ssids_for_standalone(db, device, ap, ssid_rows, count_by_idx, now)
    db.commit()
    db.refresh(ap)
    return {"client_count": total_clients, "ssid_count": ssid_count}


def find_matching_device_for_ap(db: Session, aps: list) -> dict[str, dict]:
    """Bulk lookup: for each AP whose IP (management_ip or ap_ip_address)
    matches an existing managed Device, return that Device's id/hostname.

    Lets the Wireless page point "Config backup" at NetGuard's existing
    snapshot/drift pipeline (ProtocolManager, Configuration tab, Drift
    page -- all already built and vendor-aware) instead of a dead-end
    "not supported" message whenever an AP happens to also be imported
    as a real Device with SSH/NETCONF credentials -- which for a
    Ruckus/TP-Link Omada fleet using CLI-over-SSH APs is the common case
    once someone bothers to import them. Device has no MAC column, so
    this joins on IP rather than mac_address (unlike the switchport
    correlation above, which has to use MAC since an AP's LLDP neighbor
    row has no IP at all).
    """
    from app.models.device import Device

    ips = {ap.management_ip or ap.ap_ip_address for ap in aps if (ap.management_ip or ap.ap_ip_address)}
    if not ips:
        return {}
    devices = db.query(Device).filter(Device.ip_address.in_(ips)).all()
    device_by_ip = {d.ip_address: d for d in devices}
    result: dict[str, dict] = {}
    for ap in aps:
        ip = ap.management_ip or ap.ap_ip_address
        dev = device_by_ip.get(ip) if ip else None
        if dev:
            result[str(ap.id)] = {"device_id": str(dev.id), "hostname": dev.hostname}
    return result


# DiscoveredHost.vendor_guess strings (network_discovery_service._guess_vendor,
# a mix of _SYSDESCR_VENDOR_KEYWORDS values and OUI vendor names) that
# map onto WirelessAP.vendor / WIRELESS_AP_VENDORS. Deliberately a
# separate, narrower map from _AP_SYSDESCR_MARKERS above: that one only
# needs to notice "this is *some* AP", this one needs to land on one of
# the exact WIRELESS_AP_VENDORS values the CRUD API accepts.
_DISCOVERY_VENDOR_TO_AP_VENDOR = {
    "cisco": "cisco", "aironet": "cisco",
    "aruba": "aruba",
    "ruckus": "ruckus",
    "tplink": "tplink", "tp-link": "tplink", "omada": "tplink",
    "ubiquiti": "ubiquiti", "unifi": "ubiquiti",
    "mikrotik": "mikrotik",
}


def import_ap_from_discovered_host(db: Session, host) -> "WirelessAP":
    """Creates a manually-tracked WirelessAP from a DiscoveredHost found
    by a Discovery scan, pre-filling vendor/model from the same
    sysDescr/OUI guess Discovery already computed (host.vendor_guess,
    host.snmp_sys_descr) instead of an operator retyping what NetGuard
    already knows. Mirrors app.api.network_discovery.import_host's
    "flip imported=True, point at the created row" bookkeeping, but for
    the wireless inventory instead of the general Device inventory --
    for a standalone Ruckus/TP-Link Omada AP with no WLC, this is the
    only path onto the Wireless page short of typing it in by hand.

    Raises ValueError if this host was already imported (as either an
    AP or, via the general Device import path, an ordinary Device --
    either one means someone already actioned this row).
    """
    from app.models.wireless import WirelessAP

    if host.imported:
        raise ValueError("This host has already been imported")

    sys_descr = (host.snmp_sys_descr or "").lower()
    guess = (host.vendor_guess or "").lower()
    vendor = _DISCOVERY_VENDOR_TO_AP_VENDOR.get(guess)
    if not vendor:
        for marker in _AP_SYSDESCR_MARKERS:
            if marker in sys_descr:
                vendor = _DISCOVERY_VENDOR_TO_AP_VENDOR.get(marker.replace("-", ""), None)
                break
    vendor = vendor or "other"

    # sysDescr often reads like "Ruckus R650 Multimedia Hotzone Wireless
    # AP" -- best-effort first-two-words-after-vendor-name model guess,
    # left for the operator to correct rather than blocked on getting it
    # exactly right (same "never invent, never fail the import" posture
    # as _guess_vendor itself).
    model_guess = None
    if host.snmp_sys_descr:
        words = host.snmp_sys_descr.split()
        for i, w in enumerate(words):
            if w.lower() in _AP_SYSDESCR_MARKERS or vendor in w.lower():
                model_guess = " ".join(words[i + 1:i + 3]) or None
                break

    ap = WirelessAP(
        id=uuid.uuid4(),
        controller_device_id=None,
        ap_index=None,
        ap_name=host.snmp_sys_name or host.hostname or host.ip_address,
        ap_model=model_guess,
        ap_ip_address=host.ip_address,
        vendor=vendor,
        mac_address=normalize_mac(host.mac_address),
        management_ip=host.ip_address,
        notes=f"Imported from Discovery scan (sysDescr: {host.snmp_sys_descr})" if host.snmp_sys_descr else "Imported from Discovery scan",
        source="manual",
    )
    db.add(ap)
    host.imported = True
    db.commit()
    db.refresh(ap)
    return ap


def _sync_client_sessions(
    db: Session, device, sta_ap_mac: dict[str, str], sta_phy: dict[str, str], ap_mac: dict[str, str], now
) -> None:
    """Upserts WirelessClientSession from this poll's bsnMobileStationAPMacAddr
    walk (already fetched by poll_wireless_controller for the per-band
    split). A client whose ap_index changed since last poll is treated as
    a roam: first_seen_on_ap resets to now. A client not seen this poll
    is left alone (see WirelessClientSession's docstring -- it just goes
    stale rather than being deleted), so get_sticky_clients naturally
    stops seeing it once its last_seen falls out of whatever window a
    caller cares about.
    """
    from app.models.wireless import WirelessClientSession

    if not sta_ap_mac:
        return
    mac_to_ap_index = {mac: idx for idx, mac in ap_mac.items()}

    existing = {
        row.client_mac: row
        for row in db.query(WirelessClientSession).filter_by(controller_device_id=device.id).all()
    }

    for sta_idx, ap_mac_addr in sta_ap_mac.items():
        # bsnMobileStationTable is indexed by the client's own MAC, same
        # as bsnAPDot3MacAddress is for bsnAPTable -- normalize the same
        # way, falling back to the raw index string if it doesn't parse
        # as 12 hex digits (some SNMP libraries return the client MAC as
        # a dotted-decimal OID suffix rather than a hex string) so a
        # session row still gets created rather than silently dropped.
        client_mac = normalize_mac(sta_idx) or str(sta_idx)
        target_ap_index = mac_to_ap_index.get(ap_mac_addr)
        band = _PHY_TYPE_BAND.get(str(sta_phy.get(sta_idx, "")).strip())
        if not target_ap_index:
            continue

        row = existing.get(client_mac)
        if row is None:
            row = WirelessClientSession(
                id=uuid.uuid4(),
                controller_device_id=device.id,
                client_mac=client_mac,
                ap_index=target_ap_index,
                ap_mac_address=ap_mac_addr,
                band=band,
                first_seen_on_ap=now,
                last_seen=now,
            )
            db.add(row)
        else:
            if row.ap_index != target_ap_index:
                row.first_seen_on_ap = now  # roamed (or reassociated) -- dwell clock resets
            row.ap_index = target_ap_index
            row.ap_mac_address = ap_mac_addr
            row.band = band
            row.last_seen = now
    db.commit()


def get_sticky_clients(
    db: Session, controller_device_id, min_dwell_minutes: int = 30, util_threshold_pct: int = 70
) -> list[dict]:
    """Flags clients that have dwelled on their current AP for at least
    `min_dwell_minutes` while that AP's radio (matching the client's
    band) is running at or above `util_threshold_pct` channel
    utilization -- i.e. "stuck on a busy AP instead of roaming to
    something less loaded", the practical, MIB-available proxy for
    stickiness described in the module docstring above (see
    WirelessClientSession -- true stickiness detection needs multi-AP
    RSSI-to-the-same-client, which AireOS's SNMP tables don't expose;
    long dwell + high load on the AP actually holding the client is the
    signal available here). For each flagged client, also returns every
    *other* AP on the same controller with a currently lower
    utilization on that band, as a candidate the client should have
    roamed to -- best-effort, not a real RF-proximity suggestion since
    proximity isn't derivable from this MIB either.

    Returns one dict per sticky client: client_mac, ap_id, ap_name,
    band, dwell_minutes, current_util_pct, candidate_aps (list of
    {ap_id, ap_name, util_pct} sorted least-loaded first).
    """
    import datetime as _dt

    from app.models.wireless import WirelessClientSession

    now = _dt.datetime.now(_dt.timezone.utc)
    sessions = (
        db.query(WirelessClientSession)
        .filter_by(controller_device_id=controller_device_id)
        .all()
    )
    if not sessions:
        return []

    aps = get_aps_for_controller(db, controller_device_id)
    ap_by_index = {ap.ap_index: ap for ap in aps if ap.ap_index is not None}

    findings = []
    for session in sessions:
        ap = ap_by_index.get(session.ap_index)
        if ap is None or not session.band:
            continue
        util = ap.channel_util_2g if session.band == "2g" else ap.channel_util_5g
        if util is None or util < util_threshold_pct:
            continue

        first_seen = session.first_seen_on_ap
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=_dt.timezone.utc)
        dwell_minutes = (now - first_seen).total_seconds() / 60
        if dwell_minutes < min_dwell_minutes:
            continue

        candidates = []
        for other in aps:
            if other.id == ap.id:
                continue
            other_util = other.channel_util_2g if session.band == "2g" else other.channel_util_5g
            if other_util is not None and other_util < util:
                candidates.append({"ap_id": str(other.id), "ap_name": other.ap_name, "util_pct": other_util})
        candidates.sort(key=lambda c: c["util_pct"])

        findings.append({
            "client_mac": session.client_mac,
            "ap_id": str(ap.id),
            "ap_name": ap.ap_name,
            "band": session.band,
            "dwell_minutes": round(dwell_minutes, 1),
            "current_util_pct": util,
            "candidate_aps": candidates[:5],
        })
    return findings


def get_co_channel_report(db: Session, controller_device_id=None) -> list[dict]:
    """Groups APs that likely interfere with each other: same radio
    channel + physically adjacent, where "adjacent" is approximated by
    "plugged into the same access switch" (find_switchports_for_aps'
    LLDP correlation) rather than true RF proximity/geolocation, which
    NetGuard has no source for. APs on the same wiring-closet switch are
    a reasonable, already-available proxy for "same floor/area" in most
    campus layouts -- weaker than a real site survey, good enough to
    catch the common self-inflicted case of two nearby APs both left on
    a factory-default or manually-picked channel.

    Optionally scoped to one controller; otherwise reports across every
    polled/managed AP NetGuard knows about (co-channel interference
    between two different WLCs' APs on the same switch is exactly the
    kind of thing a per-controller view would miss).

    Returns one dict per (switch, band, channel) group with >1 AP:
    switch_hostname, band, channel, aps (list of {ap_id, ap_name}).
    """
    from app.models.wireless import WirelessAP

    q = db.query(WirelessAP)
    if controller_device_id is not None:
        q = q.filter(WirelessAP.controller_device_id == controller_device_id)
    aps = q.all()
    if not aps:
        return []

    switchports = find_switchports_for_aps(db, aps)

    groups: dict[tuple[str, str, int], list] = {}
    for ap in aps:
        correlation = switchports.get(str(ap.id))
        if not correlation:
            continue  # no physical-adjacency proxy available for this AP -- can't group it
        switch_hostname = correlation["hostname"]
        for band, channel in (("2g", ap.channel_2g), ("5g", ap.channel_5g)):
            if channel is None:
                continue
            key = (switch_hostname, band, channel)
            groups.setdefault(key, []).append({"ap_id": str(ap.id), "ap_name": ap.ap_name})

    findings = []
    for (switch_hostname, band, channel), members in groups.items():
        if len(members) < 2:
            continue
        findings.append({
            "switch_hostname": switch_hostname,
            "band": band,
            "channel": channel,
            "aps": members,
        })
    findings.sort(key=lambda f: (-len(f["aps"]), f["switch_hostname"]))
    return findings


def build_snmp_auth_from_ap(ap) -> "SnmpAuthConfig | None":
    """Resolve a standalone AP's OWN SNMP credentials (WirelessAP.snmp_*
    columns) into a usable SnmpAuthConfig, or None if the AP has none set.

    This is what lets poll_standalone_ap work for a manually-added AP
    with no separately-registered Device at the same IP -- see that
    function's docstring for the duplicate-entry problem this replaces.
    Only v1/v2c (community) or v3 combinations that are actually usable
    are returned; a half-configured v3 AP (username but no auth_key
    where security_level requires one) returns None so the caller falls
    back to the legacy matching-Device path rather than attempting SNMP
    with an incomplete config.
    """
    from app.core import crypto
    from app.services.snmp_service import SnmpAuthConfig

    version = (ap.snmp_version or "").lower()
    if version not in ("v1", "v2c", "v3"):
        return None

    if version in ("v1", "v2c"):
        if not ap.snmp_community_encrypted:
            return None
        community = crypto.decrypt(ap.snmp_community_encrypted)
        if not community:
            return None
        return SnmpAuthConfig(version=version, community=community, port=ap.snmp_port or 161)

    # v3
    if not ap.snmp_username:
        return None
    security_level = ap.snmp_security_level or "noAuthNoPriv"
    auth_key = crypto.decrypt(ap.snmp_auth_key_encrypted) if ap.snmp_auth_key_encrypted else None
    priv_key = crypto.decrypt(ap.snmp_priv_key_encrypted) if ap.snmp_priv_key_encrypted else None
    if security_level in ("authNoPriv", "authPriv") and not auth_key:
        return None
    if security_level == "authPriv" and not priv_key:
        return None
    return SnmpAuthConfig(
        version="v3",
        port=ap.snmp_port or 161,
        username=ap.snmp_username,
        security_level=security_level,
        auth_protocol=ap.snmp_auth_protocol,
        auth_key=auth_key,
        priv_protocol=ap.snmp_priv_protocol,
        priv_key=priv_key,
    )


def build_snmp_auth(db: Session, device):
    """Shared "resolve this device's stored SNMP config into a usable
    SnmpAuthConfig" helper -- factored out of poll_wireless_controller so
    fhrp_poe_service's on-demand checks (and any future SNMP-based
    poller) can reuse it instead of re-deriving the same secrets-lookup
    logic. Returns None if the device doesn't have SNMP enabled at all.
    """
    from app.services.snmp_service import SnmpAuthConfig

    if not device.supports_snmp:
        return None

    auth = SnmpAuthConfig(
        version=device.snmp_version or "v2c",
        community=None,  # populated below for v1/v2c
        port=device.snmp_port or 161,
        username=device.snmp_username,
        security_level=device.snmp_security_level,
        auth_protocol=device.snmp_auth_protocol,
        priv_protocol=device.snmp_priv_protocol,
    )
    try:
        from app.services.secrets_service import resolve_secret
        if device.snmp_version in ("v1", "v2c"):
            auth.community = resolve_secret(db, device.snmp_community_ref) or "public"
        if device.snmp_version == "v3":
            auth.auth_key = resolve_secret(db, device.snmp_auth_credential_ref)
            auth.priv_key = resolve_secret(db, device.snmp_privacy_credential_ref)
    except Exception:  # noqa: BLE001
        if device.snmp_version in ("v1", "v2c"):
            auth.community = "public"
    return auth


def poll_wireless_controller(db: Session, device) -> dict:
    """Walk a WLC's SNMP tables and upsert wireless_aps / wireless_ssids.

    Parameters
    ----------
    db     : active SQLAlchemy session
    device : app.models.device.Device instance with SNMP config

    Returns
    -------
    dict with keys ``ap_count``, ``ssid_count`` for logging / Celery task
    result propagation.  Both are 0 when the device is not an SNMP WLC.
    """
    from app.models.wireless import WirelessAP, WirelessSSID
    from app.services.snmp_service import _walk

    # Build SNMP auth from the device's stored config (same as snmp_service).
    auth = build_snmp_auth(db, device)
    if auth is None:
        return {"ap_count": 0, "ssid_count": 0, "error": "snmp_disabled"}

    ip = device.ip_address
    timeout = 5.0

    # ------------------------------------------------------------------
    # AP table walk -- anchor on AP name (bsnAPName) since it's the most
    # universally populated column.  If empty → not a WLC, bail cleanly.
    # ------------------------------------------------------------------
    ap_names = _walk(ip, auth, _OID_AP_NAME, timeout)
    if not ap_names:
        logger.debug("wireless: %s returned no AireOS AP rows -- not a WLC or SNMP unreachable", ip)
        return {"ap_count": 0, "ssid_count": 0}

    ap_oper = _walk(ip, auth, _OID_AP_OPER, timeout)
    ap_model = _walk(ip, auth, _OID_AP_MODEL, timeout)
    ap_ip = _walk(ip, auth, _OID_AP_IP, timeout)
    ap_clients = _walk(ip, auth, _OID_AP_CLIENTS, timeout)
    ap_mac = _walk(ip, auth, _OID_AP_MAC, timeout)
    ap_uptime = _walk(ip, auth, _OID_AP_UPTIME, timeout)
    ap_sw_version = _walk(ip, auth, _OID_AP_SW_VERSION, timeout)
    ap_serial = _walk(ip, auth, _OID_AP_SERIAL, timeout)

    # Per-radio (2.4GHz / 5GHz) channel/power/noise/utilization, keyed by
    # AP index. bsnAPIfTable rows are indexed "<ap_index>.<slot>" -- see
    # _RADIO_SLOT_BAND above. Best-effort: a controller/firmware that
    # doesn't expose one of these columns just leaves those fields None
    # on the AP row, same contract as everything else in this poll.
    radio_by_ap_index: dict[str, dict[str, dict[str, int]]] = {}
    try:
        radio_channel = _walk(ip, auth, _OID_RADIO_CHANNEL, timeout)
        radio_tx_power = _walk(ip, auth, _OID_RADIO_TX_POWER, timeout)
        radio_noise = _walk(ip, auth, _OID_RADIO_NOISE, timeout)
        radio_util = _walk(ip, auth, _OID_RADIO_UTIL, timeout)
        radio_rows = set(radio_channel) | set(radio_tx_power) | set(radio_noise) | set(radio_util)
        for radio_idx in radio_rows:
            if "." not in radio_idx:
                continue
            ap_idx, _, slot = radio_idx.rpartition(".")
            band = _RADIO_SLOT_BAND.get(slot)
            if not band:
                continue
            per_ap = radio_by_ap_index.setdefault(ap_idx, {})
            per_ap[band] = {
                "channel": _safe_int(radio_channel.get(radio_idx)),
                "tx_power": _safe_int(radio_tx_power.get(radio_idx)),
                "noise": _safe_int(radio_noise.get(radio_idx)),
                "util": _safe_int(radio_util.get(radio_idx)),
            }
    except Exception:  # noqa: BLE001
        logger.debug("wireless: per-radio telemetry walk failed for %s", ip, exc_info=True)
        radio_by_ap_index = {}

    # Per-band client split (2.4GHz / 5GHz), keyed by AP index via AP MAC.
    # Best-effort and bounded -- see _MAX_CLIENTS_WALKED above. Never lets
    # a slow/partial client-table walk take down the AP/SSID poll it's
    # layered on top of.
    band_by_ap_index: dict[str, dict[str, int]] = {}
    sta_ap_mac: dict[str, str] = {}
    sta_phy: dict[str, str] = {}
    try:
        mac_to_ap_index = {mac: idx for idx, mac in ap_mac.items()}
        sta_ap_mac = _walk(ip, auth, _OID_STA_AP_MAC, timeout)
        if len(sta_ap_mac) > _MAX_CLIENTS_WALKED:
            logger.info(
                "wireless: %s has %d associated clients, capping band-split walk at %d",
                ip, len(sta_ap_mac), _MAX_CLIENTS_WALKED,
            )
        if sta_ap_mac and mac_to_ap_index:
            sta_phy = _walk(ip, auth, _OID_STA_PHY_TYPE, timeout)
            for sta_idx, ap_mac_addr in list(sta_ap_mac.items())[:_MAX_CLIENTS_WALKED]:
                target_ap_index = mac_to_ap_index.get(ap_mac_addr)
                band = _PHY_TYPE_BAND.get(str(sta_phy.get(sta_idx, "")).strip())
                if not target_ap_index or not band:
                    continue
                counts = band_by_ap_index.setdefault(target_ap_index, {"2g": 0, "5g": 0})
                counts[band] += 1
    except Exception:  # noqa: BLE001
        logger.debug("wireless: per-band client split failed for %s", ip, exc_info=True)
        band_by_ap_index = {}

    now = datetime.datetime.now(datetime.timezone.utc)
    ap_count = 0

    for idx, name in ap_names.items():
        existing = (
            db.query(WirelessAP)
            .filter_by(controller_device_id=device.id, ap_index=idx)
            .first()
        )
        if existing is None:
            existing = WirelessAP(
                id=uuid.uuid4(),
                controller_device_id=device.id,
                ap_index=idx,
                source="polled",
                vendor="cisco",
            )
            db.add(existing)

        existing.source = "polled"

        existing.ap_name = str(name).strip() or None
        existing.ap_model = ap_model.get(idx)
        existing.ap_ip_address = ap_ip.get(idx)
        # Previously never set for polled rows even though bsnAPDot3MacAddress
        # was already being walked (for the radio-table join above) --
        # this is also what the Wireless page's AP-to-switchport
        # correlation (find_switchport_for_ap) and the "already in
        # inventory as a Device" lookup join against, so a polled AP with
        # no mac_address couldn't be correlated to anything even though
        # the data was sitting right there.
        existing.mac_address = normalize_mac(ap_mac.get(idx)) or existing.mac_address
        existing.oper_status = _safe_int(ap_oper.get(idx))
        existing.client_count = _safe_int(ap_clients.get(idx))
        band_counts = band_by_ap_index.get(idx)
        existing.band_2g_clients = band_counts["2g"] if band_counts else None
        existing.band_5g_clients = band_counts["5g"] if band_counts else None

        existing.ap_up_time = str(ap_uptime.get(idx)).strip() if ap_uptime.get(idx) is not None else None
        existing.ap_software_version = ap_sw_version.get(idx)
        existing.ap_serial_number = ap_serial.get(idx)

        radios = radio_by_ap_index.get(idx, {})
        radio_2g = radios.get("2g", {})
        radio_5g = radios.get("5g", {})
        existing.channel_2g = radio_2g.get("channel")
        existing.channel_5g = radio_5g.get("channel")
        existing.tx_power_2g = radio_2g.get("tx_power")
        existing.tx_power_5g = radio_5g.get("tx_power")
        existing.noise_2g = radio_2g.get("noise")
        existing.noise_5g = radio_5g.get("noise")
        existing.channel_util_2g = radio_2g.get("util")
        existing.channel_util_5g = radio_5g.get("util")
        existing.polled_at = now
        ap_count += 1

    # ------------------------------------------------------------------
    # SSID table walk
    # ------------------------------------------------------------------
    ess_ssids = _walk(ip, auth, _OID_ESS_SSID, timeout)
    ess_admin = _walk(ip, auth, _OID_ESS_ADMIN, timeout)
    ess_clients = _walk(ip, auth, _OID_ESS_CLIENTS, timeout)
    ess_wep = _walk(ip, auth, _OID_ESS_WEP_STATE, timeout)
    ess_wpa1 = _walk(ip, auth, _OID_ESS_WPA1_ENABLE, timeout)
    ess_wpa2 = _walk(ip, auth, _OID_ESS_WPA2_ENABLE, timeout)

    ssid_count = 0
    for idx, ssid_name in ess_ssids.items():
        ssid_str = str(ssid_name).strip()
        if not ssid_str:
            continue
        existing = (
            db.query(WirelessSSID)
            .filter_by(controller_device_id=device.id, ssid_index=idx)
            .first()
        )
        if existing is None:
            existing = WirelessSSID(
                id=uuid.uuid4(),
                controller_device_id=device.id,
                ssid_index=idx,
                ssid_name=ssid_str,
            )
            db.add(existing)

        security_mode, is_weak = _classify_ssid_security(
            ess_wep.get(idx), ess_wpa1.get(idx), ess_wpa2.get(idx)
        )

        existing.ssid_name = ssid_str
        existing.admin_status = _safe_int(ess_admin.get(idx))
        existing.mobile_station_count = _safe_int(ess_clients.get(idx))
        existing.security_mode = security_mode
        existing.is_weak_security = is_weak
        existing.polled_at = now
        ssid_count += 1

    db.commit()
    logger.info("wireless: polled %s → %d APs, %d SSIDs", ip, ap_count, ssid_count)

    # Push this poll's per-AP gauges to VictoriaMetrics for historical
    # trending -- wireless_aps itself only ever holds the latest
    # snapshot (see the module docstring on app.models.wireless), so
    # this is the only place "was this AP degraded at 2pm yesterday" can
    # be answered from. Best-effort, same as the alert-rule evaluation
    # below: a VM hiccup must never make an otherwise-successful poll
    # look like it failed.
    try:
        from app.core import vm_client
        polled_aps = get_aps_for_controller(db, device.id)
        vm_client.write_ap_polls(
            device.id,
            device.hostname,
            now,
            [
                {
                    "ap_id": str(ap.id),
                    "ap_name": ap.ap_name,
                    "client_count": ap.client_count,
                    "band_2g_clients": ap.band_2g_clients,
                    "band_5g_clients": ap.band_5g_clients,
                    "channel_util_2g": ap.channel_util_2g,
                    "channel_util_5g": ap.channel_util_5g,
                    "noise_2g": ap.noise_2g,
                    "noise_5g": ap.noise_5g,
                    "tx_power_2g": ap.tx_power_2g,
                    "tx_power_5g": ap.tx_power_5g,
                }
                for ap in polled_aps
                if ap.ap_index is not None  # only this poll's rows, not other manual/stale ones
            ],
        )
    except Exception:  # noqa: BLE001
        logger.debug("wireless: VictoriaMetrics AP write failed for %s", ip, exc_info=True)

    # Per-client AP association, for sticky-client detection -- see
    # _sync_client_sessions below. Reuses sta_ap_mac/sta_phy, already
    # walked above for the per-band client split, so this costs no extra
    # SNMP round trips.
    try:
        _sync_client_sessions(db, device, sta_ap_mac, sta_phy, ap_mac, now)
    except Exception:  # noqa: BLE001
        logger.debug("wireless: client-session sync failed for %s", ip, exc_info=True)

    # Custom Alert Rules on per-AP channel utilization / noise -- see
    # alert_rule_engine.evaluate_ap_rules. Best-effort: a bad rule or an
    # alert-pipeline hiccup here must never make the poll itself look
    # like it failed, since the AP/SSID data above already committed.
    try:
        from app.services.alert_rule_engine import evaluate_ap_rules
        evaluate_ap_rules(db, device)
    except Exception:  # noqa: BLE001
        logger.debug("wireless: AP alert-rule evaluation failed for %s", ip, exc_info=True)

    return {"ap_count": ap_count, "ssid_count": ssid_count}


def get_aps_for_controller(db: Session, controller_device_id) -> list:
    """Return all WirelessAP rows for a given controller, most-recently
    polled first."""
    from app.models.wireless import WirelessAP
    return (
        db.query(WirelessAP)
        .filter_by(controller_device_id=str(controller_device_id))
        .order_by(WirelessAP.polled_at.desc(), WirelessAP.ap_name)
        .all()
    )


def get_ssids_for_controller(db: Session, controller_device_id) -> list:
    """Return all WirelessSSID rows for a given controller."""
    from app.models.wireless import WirelessSSID
    return (
        db.query(WirelessSSID)
        .filter_by(controller_device_id=str(controller_device_id))
        .order_by(WirelessSSID.ssid_name)
        .all()
    )


def get_wireless_summary(db: Session, controller_device_id, controller_hostname: str | None = None) -> dict:
    """Aggregate snapshot stats for a single controller."""

    aps = get_aps_for_controller(db, controller_device_id)
    ssids = get_ssids_for_controller(db, controller_device_id)

    aps_up = sum(1 for a in aps if a.oper_status == 1)
    total_clients = sum(a.client_count or 0 for a in aps)
    band_2g = sum(a.band_2g_clients or 0 for a in aps)
    band_5g = sum(a.band_5g_clients or 0 for a in aps)
    polled_at = max((a.polled_at for a in aps), default=None) if aps else None

    return {
        "controller_device_id": str(controller_device_id),
        "controller_hostname": controller_hostname,
        "total_aps": len(aps),
        "aps_up": aps_up,
        "aps_down": len(aps) - aps_up,
        "total_clients": total_clients,
        "band_2g_clients": band_2g,
        "band_5g_clients": band_5g,
        "ssid_count": len(ssids),
        "polled_at": polled_at,
    }
