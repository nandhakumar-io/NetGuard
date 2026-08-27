"""SNMP monitoring service (pysnmp).

Powers the SNMP Health Dashboard: polls a device's standard MIB-II /
vendor CPU-MEM-MIB-style OIDs, turns the raw readings into a 0-100 health
score + green/yellow/red classification, and flags threshold breaches for
the Alert Engine. Also parses inbound SNMP traps (POST /snmp/traps) into
the same category vocabulary the Alert Engine uses for polled breaches, so
"Interface Down" looks the same in the UI whether it came from a trap or
from a poll that noticed the interface was down.
"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

logger = logging.getLogger("netguard.snmp")

# Standard OIDs used for the health poll. Kept generic (MIB-II + the
# widely-implemented CISCO-PROCESS-MIB/HOST-RESOURCES-MIB equivalents)
# rather than vendor-specific so the same poller works across
# cisco/juniper/arista/linux; a real deployment can extend this map
# per-vendor without changing the calling code.
OIDS = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",  # scalar -- used only by test_connection() below
    "sysName": "1.3.6.1.2.1.1.5.0",  # scalar -- device's configured hostname, used by discover_inventory()
    "sysUpTime": "1.3.6.1.2.1.1.3.0",  # scalar (.0 instance) -- GET works as-is
    # cpu_5min / mem_used / mem_free / temperature are all *table columns*
    # (indexed by CPU/pool/sensor number), not scalars. An SNMP GET needs a
    # concrete row instance appended -- GETting the bare column OID returns
    # "No Such Instance" from the agent, which _get_via_pysnmp treats as a
    # normal failure (returns None). That's why only sysUpTime (a real
    # scalar) was ever coming back. ".1" is the first/only row on the vast
    # majority of single-CPU, single-processor-pool, single-sensor devices
    # (including the IOSv images used in the GNS3 lab); a fleet with
    # multi-instance chassis would need a per-device index lookup instead.
    # CISCO-PROCESS-MIB cpmCPUTotalTable (1.3.6.1.4.1.9.9.109.1.1.1.1),
    # "Rev" variants -- .6/.7/.8 respectively. These were previously
    # mislabeled: "cpu_5min" pointed at .6.1 (actually the 5-SECOND
    # reading, cpmCPUTotal5secRev), with the real 5-minute OID (.8.1)
    # only ever tried as a last-resort *fallback* -- so every CPU
    # health-score/threshold-alert calculation was silently working off a
    # noisy instantaneous spot value instead of the smoothed 5-min
    # average, risking spiky false-positive "High CPU" alerts. Now all
    # three are correctly separated; poll_health uses cpu_5min (the
    # smoothed value) as the canonical health/alerting figure, same as
    # before the fix was needed -- cpu_5sec/cpu_1min are captured too for
    # future finer-grained display but aren't part of alerting yet.
    # NOTE: these four used to be hardcoded to row instance ".1"
    # (cpmCPUTotal5minRev.1, ciscoMemoryPoolUsed.1, ...) on the assumption
    # that a single-CPU/single-pool/single-sensor device always lives at
    # row index 1. That assumption is false in practice -- a real device
    # tested with `snmpwalk -v2c -c public <ip>
    # 1.3.6.1.4.1.9.9.109.1.1.1.1.6` returned
    # ...109.1.1.1.1.6.7 = Gauge32: 20, i.e. row index *7*, not 1.
    # GETting the ".1" instance on that device returns "No Such Instance",
    # which _get_via_pysnmp treats as a normal (silent) failure -- so CPU/
    # mem/temp/fan/power all quietly came back empty. These are now stored
    # as *base* column OIDs (no trailing instance) and resolved via
    # _get_first_table_value(), which walks the column and uses whichever
    # row index the agent actually returns first -- 1, 7, or anything else.
    "cpu_5sec": "1.3.6.1.4.1.9.9.109.1.1.1.1.6",   # cpmCPUTotal5secRev
    "cpu_1min": "1.3.6.1.4.1.9.9.109.1.1.1.1.7",   # cpmCPUTotal1minRev
    "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.8",   # cpmCPUTotal5minRev (the correct, smoothed value)
    "mem_used": "1.3.6.1.4.1.9.9.48.1.1.1.5",  # ciscoMemoryPoolUsed (first pool row returned)
    "mem_free": "1.3.6.1.4.1.9.9.48.1.1.1.6",  # ciscoMemoryPoolFree (same row index as mem_used)
    "temperature": "1.3.6.1.4.1.9.9.13.1.3.1.3",  # ciscoEnvMonTemperatureValue
    # CISCO-ENVMON-MIB state tables (also row-indexed -- first fan tray /
    # first PSU, whatever index that turns out to be). Replaces the old
    # hardcoded fan_status="ok" / power_supply_status="ok" placeholders
    # with real device telemetry.
    "fan_state": "1.3.6.1.4.1.9.9.13.1.4.1.3",  # ciscoEnvMonFanState
    "power_supply_state": "1.3.6.1.4.1.9.9.13.1.5.1.3",  # ciscoEnvMonSupplyState
}

# Fallback CPU OIDs for devices where cpmCPUTotal5minRev (CISCO-PROCESS-MIB,
# the primary "cpu_5min" OID above) isn't implemented -- older Cisco images
# and non-Cisco gear. Each of these is also a 5-minute-equivalent (or, for
# avgBusy5, effectively the closest legacy analog), not a 5-second spot
# reading, so falling back to one of these preserves the same "smoothed,
# not spiky" semantics cpu_5min is meant to have.
CPU_FALLBACK_OIDS = [
    "1.3.6.1.4.1.9.9.109.1.1.1.1.5.1",  # cpmCPUTotal5min.1 (older CISCO-PROCESS-MIB, pre-"Rev" table)
    "1.3.6.1.4.1.9.2.1.58.0",            # OLD-CISCO-CPU-MIB avgBusy5 (scalar, very widely supported)
    "1.3.6.1.2.1.25.3.3.1.2.1",          # HOST-RESOURCES-MIB hrProcessorLoad.1 (vendor-neutral)
]

# ciscoEnvMonState values shared by fan/PSU/temperature status tables:
# 1=normal 2=warning 3=critical 4=shutdown 5=notPresent 6=notFunctioning
ENVMON_STATE_OK = {1}
ENVMON_STATE_WARNING = {2}
ENVMON_STATE_FAILED = {3, 4, 6}
ENVMON_STATE_NOT_PRESENT = {5}

# Standard MIB-II ifTable columns (RFC 1213 / RFC 2863), walked across every
# interface index rather than GET'd on a single OID -- this is what powers
# the Interface Statistics / Errors panel on the Health Dashboard.
IFTABLE_OIDS = {
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",  # 1 = up
    "ifInErrors": "1.3.6.1.2.1.2.2.1.14",
    "ifOutErrors": "1.3.6.1.2.1.2.2.1.20",
    "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6",  # 64-bit counters (ifXTable)
    "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10",
    "ifHighSpeed": "1.3.6.1.2.1.31.1.1.1.15",  # Mbps
}
MAX_INTERFACES_WALKED = 64  # guard against a runaway walk on a chassis with hundreds of interfaces

# Retries for every GET/GETNEXT (was 1). Standard LLDP-MIB walks
# (1.0.8802...) are comparatively rarely-polled OIDs on most platforms --
# Junos in particular is measurably slower to answer them than its
# heavily-cached proprietary tables (ifTable, jnxOperatingTable). At
# retries=1, a single dropped/slow UDP response on a loaded agent was
# enough to make the whole LLDP walk come back empty (indistinguishable
# from "LLDP genuinely has no neighbors"), which read as "LLDP doesn't
# work for Juniper" even though the OIDs and walk logic were correct --
# it was a timing/retry issue, not an OID issue. One retry costs nothing
# on the common case (a healthy agent still answers on the first try) and
# meaningfully reduces false-empty results from a single missed packet.
DISCOVERY_TIMEOUT_FLOOR = 5.0  # discover_inventory() is on-demand, not the frequent poll -- can afford to wait longer

# --- Discovery OIDs (Cisco devices) ---------------------------------------
# Powers discover_inventory() below: ARP table, routing table, LLDP/CDP
# neighbors, and chassis/module inventory. These are walked on demand (the
# "Discovery" action), not on every routine health poll -- they're much
# heavier tables than ifTable and change far less often.
ARP_OIDS = {
    "ipNetToMediaPhysAddress": "1.3.6.1.2.1.4.22.1.2",  # index: ifIndex.a.b.c.d (IP embedded in suffix) -> MAC
}
# RFC 4293 IP-MIB ipNetToPhysicalTable -- the replacement for the older
# ipNetToMediaTable above. Several real devices (IOS-XE routers in
# particular) simply don't populate ipNetToMediaTable at all any more --
# it walks successfully (no error) but returns zero rows -- while this
# table has the live ARP entries. Used as a fallback when the primary
# walk comes back empty, not tried first, since ipNetToMediaTable is
# still the more universally-implemented table on older/non-Cisco gear.
# Index: ifIndex.ipNetToPhysicalNetAddressType.ipNetToPhysicalNetAddress
# (address itself is length-prefixed InetAddress: addrLen.b1.b2.b3.b4 for
# IPv4) -- i.e. ifIndex.addrType.addrLen.b1.b2.b3.b4, 7 components for a
# v4 entry (addrType=1).
ARP_OIDS_FALLBACK = {
    "ipNetToPhysicalPhysAddress": "1.3.6.1.2.1.4.35.1.4",
}
ROUTE_OIDS = {
    "ipRouteNextHop": "1.3.6.1.2.1.4.21.1.7",   # index: destination IP -> next-hop IP
    "ipRouteMask": "1.3.6.1.2.1.4.21.1.11",     # index: destination IP -> subnet mask
    "ipRouteIfIndex": "1.3.6.1.2.1.4.21.1.2",   # index: destination IP -> outgoing ifIndex
}
# IP-FORWARD-MIB ipCidrRouteTable -- the replacement for the deprecated
# ipRouteTable above; same story as the ARP fallback, ipRouteTable often
# walks clean but empty on modern IOS-XE. Index is the composite
# dest(4).mask(4).tos(1).nexthop(4) -- 13 components -- so destination,
# mask, and next-hop are all recoverable straight from the index itself;
# only ifIndex needs a separate column walk.
ROUTE_OIDS_FALLBACK = {
    "ipCidrRouteIfIndex": "1.3.6.1.2.1.4.24.4.1.5",
}
LLDP_OIDS = {
    # Index: lldpRemTimeMark.lldpRemLocalPortNum.lldpRemIndex
    #
    # lldpRemChassisId is used as the *anchor* walk (see
    # _discover_lldp_neighbors below) instead of lldpRemSysName: per the
    # LLDP-MIB / IEEE 802.1AB spec, System Name is an OPTIONAL TLV, while
    # Chassis ID is mandatory on every advertised neighbor. Cisco IOS
    # sends the System Name TLV by default, so anchoring on sysName
    # happened to work for Cisco neighbors; Junos frequently doesn't
    # populate lldpRemSysName even though the neighbor row (and its
    # mandatory chassis/port IDs) is very much present -- anchoring on it
    # made every Juniper-adjacent LLDP walk come back empty even with
    # LLDP fully enabled and neighbors up.
    "lldpRemChassisIdSubtype": "1.0.8802.1.1.2.1.4.1.1.4",
    "lldpRemChassisId": "1.0.8802.1.1.2.1.4.1.1.5",
    "lldpRemSysName": "1.0.8802.1.1.2.1.4.1.1.9",
    "lldpRemPortId": "1.0.8802.1.1.2.1.4.1.1.7",
    # Needed to interpret lldpRemPortId correctly -- see
    # _discover_lldp_neighbors. Without this, a neighbor advertising its
    # port as a raw ifIndex/MAC (subtype local/macAddress, common on
    # Juniper) rendered as a meaningless bare number instead of being
    # left for the caller to resolve, or shown misleadingly.
    "lldpRemPortIdSubtype": "1.0.8802.1.1.2.1.4.1.1.6",
    "lldpRemSysDesc": "1.0.8802.1.1.2.1.4.1.1.10",
    # lldpLocPortTable, index: lldpLocPortNum -- this is the same local
    # port number lldpRemTable's 2nd index component refers to, but
    # lldpLocPortNum is an *internal LLDP agent* port handle, not an
    # ifIndex and not a human-readable name on its own. lldpLocPortId is
    # the actual configured local-port identifier: when
    # lldpLocPortIdSubtype == 3 (interfaceName, the common case on both
    # Junos and IOS/IOS-XE), lldpLocPortId *is* the real interface name
    # string (e.g. "ge-0/0/0", "GigabitEthernet1/0/1") -- exactly what
    # was missing here before, which is why the Discovery tab's "Local
    # Port" column showed raw LLDP port-number bookkeeping values
    # instead of real ports.
    "lldpLocPortIdSubtype": "1.0.8802.1.1.2.1.3.7.1.2",
    "lldpLocPortId": "1.0.8802.1.1.2.1.3.7.1.3",
}
# lldpLocPortIdSubtype/lldpRemPortIdSubtype value meaning "the port ID is
# an interface name string". IEEE 802.1AB's LldpPortIdSubtype enumeration
# is: interfaceAlias(1), portComponent(2), macAddress(3), networkAddress(4),
# interfaceName(5), agentCircuitId(6), local(7). This was previously set to
# "3" (macAddress) instead of "5" (interfaceName) -- since almost no real
# device tags its port ID subtype as macAddress(3), that off-by-two bug
# meant the interfaceName == "3" check essentially never matched, so both
# the local *and* remote "Local Port"/"Neighbor Port" columns fell back to
# raw LLDP port-number/ifIndex bookkeeping values (e.g. "550", "535")
# instead of resolving to real interface names (e.g. "ge-0/0/22") on
# Junos and IOS/IOS-XE, which both report subtype interfaceName(5) by
# default.
LLDP_PORT_SUBTYPE_IFNAME = "5"
# Kept as an alias -- this constant used to be local-port-only before
# remote-port subtype-aware resolution was added below.
LLDP_LOC_PORT_SUBTYPE_IFNAME = LLDP_PORT_SUBTYPE_IFNAME
# local(7): the port ID value is the device's own ifIndex, not a name --
# see _resolve_lldp_local_port_names' docstring for why Junos needs this
# handled for the *local* side too, not just the remote side.
LLDP_PORT_SUBTYPE_LOCAL = "7"
CDP_OIDS = {
    # Index: ifIndex.cdpCacheDeviceIndex
    "cdpCacheDeviceId": "1.3.6.1.4.1.9.9.23.1.2.1.1.6",
    "cdpCacheDevicePort": "1.3.6.1.4.1.9.9.23.1.2.1.1.7",
    "cdpCachePlatform": "1.3.6.1.4.1.9.9.23.1.2.1.1.8",
}
INVENTORY_OIDS = {
    # ENTITY-MIB entPhysicalTable, index: entPhysicalIndex
    "entPhysicalDescr": "1.3.6.1.2.1.47.1.1.1.1.2",
    "entPhysicalClass": "1.3.6.1.2.1.47.1.1.1.1.5",  # 3 = chassis(3) -- see PHYSICAL_CLASS_CHASSIS below
    "entPhysicalName": "1.3.6.1.2.1.47.1.1.1.1.7",
    "entPhysicalSerialNum": "1.3.6.1.2.1.47.1.1.1.1.11",
    "entPhysicalModelName": "1.3.6.1.2.1.47.1.1.1.1.13",
}
# ENTITY-MIB PhysicalClass enum value for "chassis" -- used to pick the
# single row that represents the whole device (for the Overview page's
# Model/Serial Number fields) out of entPhysicalTable, which typically
# also has dozens of other rows for slots, ports, power supplies, fans,
# etc. that each have their own (irrelevant, sub-component) serial/model.
PHYSICAL_CLASS_CHASSIS = "3"
MAX_DISCOVERY_ROWS = 128  # guard against runaway walks on tables that can legitimately be huge (ARP, routes)

# JUNIPER-MIB scalars (jnxBoxAnatomy) -- fallback for platform/model/serial
# when ENTITY-MIB comes back empty. Junos *does* implement entPhysicalTable
# on real hardware, but a lot of the virtual/lab images NetGuard gets
# pointed at in practice (vMX, vEX, vSRX under GNS3/vagrant) either don't
# populate it at all or only populate a handful of rows with no class 3
# (chassis) row -- so _detect_chassis_summary's ENTITY-MIB walk comes back
# with candidates=[] and Overview's Model/Serial Number stay blank forever
# for exactly the vendor whose lab images are least likely to have a fully
# populated ENTITY-MIB. jnxBoxDescr/jnxBoxSerialNo are scalars (not a
# table), present on every Junos SNMP agent NetGuard has seen respond at
# all, so they're a much more reliable source for this vendor specifically.
JUNIPER_BOX_OIDS = {
    "jnxBoxDescr": "1.3.6.1.4.1.2636.3.1.2.0",
    "jnxBoxSerialNo": "1.3.6.1.4.1.2636.3.1.3.0",
}

# Threshold defaults for turning raw readings into alerts (SNMP Health
# Dashboard traffic-light + Alert Engine "High CPU" / "Temperature
# Critical" style events).
CPU_WARN_PCT = 75
CPU_CRIT_PCT = 90
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 92
TEMP_WARN_C = 60
TEMP_CRIT_C = 75
IFACE_UTIL_WARN_PCT = 80
IFACE_UTIL_CRIT_PCT = 95
IFACE_ERRORS_WARN = 100  # cumulative errors seen growing since last poll


@dataclass
class SnmpAuthConfig:
    """Everything needed to authenticate one SNMP session, v1/v2c or v3.

    v1/v2c only uses `community` (+ `version`). v3 ignores `community`
    entirely and uses `username` + the USM parameters -- `security_level`
    determines which of auth_protocol/auth_key/priv_protocol/priv_key are
    actually required:
      noAuthNoPriv -> username only
      authNoPriv   -> + auth_protocol, auth_key
      authPriv     -> + priv_protocol, priv_key
    """
    version: str  # "v1" | "v2c" | "v3"
    community: str | None = None
    port: int = 161
    username: str | None = None
    security_level: str | None = None  # noAuthNoPriv | authNoPriv | authPriv
    auth_protocol: str | None = None  # MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
    auth_key: str | None = None
    priv_protocol: str | None = None  # DES | 3DES | AES128 | AES192 | AES256
    priv_key: str | None = None


@dataclass
class SnmpMetrics:
    cpu_utilization_pct: float | None = None
    memory_utilization_pct: float | None = None
    interface_utilization_pct: float | None = None
    interface_errors: int | None = None
    temperature_celsius: float | None = None
    fan_status: str = "unknown"
    power_supply_status: str = "unknown"
    uptime_seconds: int | None = None
    reachable: bool = False
    error: str | None = None
    # Raw cumulative counters (SNMP counters are cumulative-since-boot, not
    # instantaneous) -- utilization is derived from the *delta* between two
    # polls, so these are carried alongside interface_utilization_pct and
    # persisted to VictoriaMetrics for the next poll to diff against.
    interface_octets_total: int | None = None
    interface_speed_bps: int | None = None
    interface_count: int | None = None
    # Per-interface snapshot from this poll (index, descr, oper "up"/"down",
    # octets/speed/errors -- octets/speed/errors are None for down
    # interfaces, which don't factor into the fleet rollups above). Every
    # interface the walk found, not just the operationally-up ones, so
    # metrics_service can detect down ports and raise/clear "Interface
    # Down" alerts. Defaults to an empty list rather than None so callers
    # never need a null check before iterating.
    per_interface: list[dict] = field(default_factory=list)
    # Opportunistically filled in by metrics_service.poll_device -- these
    # aren't part of the base SNMP health walk (see poll_device for why
    # each is gated) and stay None whenever the poll didn't compute them,
    # same "don't invent data we don't have" contract as the fields above.
    trunk_ports_down: int | None = None  # trunk-mode switchports currently oper-down
    sfp_ports_down: int | None = None  # down ports on likely SFP/optic-speed interfaces
    route_unreachable: bool | None = None  # True if the default route is missing from the routing table
    ping_packet_loss_pct: float | None = None  # set by reachability_service, not the SNMP poll path


_AUTH_PROTOCOL_NAMES = {
    "MD5": "usmHMACMD5AuthProtocol",
    "SHA": "usmHMACSHAAuthProtocol",
    "SHA224": "usmHMAC128SHA224AuthProtocol",
    "SHA256": "usmHMAC192SHA256AuthProtocol",
    "SHA384": "usmHMAC256SHA384AuthProtocol",
    "SHA512": "usmHMAC384SHA512AuthProtocol",
}
_PRIV_PROTOCOL_NAMES = {
    "DES": "usmDESPrivProtocol",
    "3DES": "usm3DESEDEPrivProtocol",
    "AES128": "usmAesCfb128Protocol",
    "AES192": "usmAesCfb192Protocol",
    "AES256": "usmAesCfb256Protocol",
}


def _format_snmp_value(value) -> str:
    """Safely decodes PySNMP values. Raw bytes/OctetStrings that contain
    non-printable characters are formatted as '0x...' hex strings so that
    downstream MAC address / chassis ID parsers can safely extract them
    instead of receiving garbled text."""
    if hasattr(value, "asOctets"):
        try:
            raw = value.asOctets()
            if any((b < 32 and b not in (9, 10, 13)) or b > 126 for b in raw):
                return "0x" + raw.hex()
        except Exception:
            pass
    return str(value)


def _build_usm_user_data(auth: "SnmpAuthConfig", pysnmp_asyncio_module):
    """Builds a pysnmp UsmUserData for SNMPv3, honoring security_level:
    noAuthNoPriv (username only), authNoPriv (+ auth), authPriv (+ priv).
    `pysnmp_asyncio_module` is the already-imported `pysnmp.hlapi.asyncio`
    module -- UsmUserData and the usmHMAC*/usmAes*/usmDES* protocol
    constant objects all live there alongside CommunityData (this flat
    module -- see _get_via_pysnmp's docstring on why this exact module --
    supports v1/v2c/v3 uniformly, same as classic pre-6.x pysnmp.hlapi).
    """
    m = pysnmp_asyncio_module
    kwargs: dict = {}
    if auth.security_level in ("authNoPriv", "authPriv") and auth.auth_protocol and auth.auth_key:
        kwargs["authKey"] = auth.auth_key
        kwargs["authProtocol"] = getattr(m, _AUTH_PROTOCOL_NAMES.get(auth.auth_protocol, ""), m.usmHMACSHAAuthProtocol)
    if auth.security_level == "authPriv" and auth.priv_protocol and auth.priv_key:
        kwargs["privKey"] = auth.priv_key
        kwargs["privProtocol"] = getattr(m, _PRIV_PROTOCOL_NAMES.get(auth.priv_protocol, ""), m.usmAesCfb128Protocol)
    return m.UsmUserData(auth.username, **kwargs)


def _get_via_pysnmp(ip_address: str, auth: "SnmpAuthConfig", oid: str, timeout: float) -> str | None:
    """Single SNMP GET. Returns None (not an exception) on any failure --
    an unreadable OID is "not applicable"/"unsupported", the same
    tolerant pattern health_monitor.py uses for NAPALM getters, so one
    missing OID doesn't fail the whole poll.

    Supports v1/v2c (CommunityData) and v3 (UsmUserData) through the same
    call path -- pysnmp's engine dispatches on the authData type, which is
    exactly why classic pysnmp.hlapi has always taken a single polymorphic
    authData argument instead of separate v1/v2c/v3 functions.

    NOTE: pysnmp>=6.2 removed the old synchronous `pysnmp.hlapi` generator
    API (SnmpEngine()/getCmd()/next(iterator)) -- getCmd/nextCmd are now
    coroutines. This build of 6.2.5 exposes them as a flat
    `pysnmp.hlapi.asyncio` module (camelCase getCmd/nextCmd, still takes a
    ContextData argument, UdpTransportTarget is a plain constructor) --
    NOT the v1arch/v3arch split that later pysnmp releases introduced, so
    don't "fix" this back to pysnmp.hlapi.v1arch.asyncio without checking
    `python3 -c "import pysnmp.hlapi.asyncio as m; print(dir(m))"` first,
    since that split may or may not exist depending on the exact installed
    build. We use asyncio.run() per call since this is invoked from
    synchronous Celery task context, not from inside a running event loop.
    """
    try:
        import pysnmp.hlapi.asyncio as m

        async def _run() -> str | None:
            if auth.version == "v3":
                auth_data = _build_usm_user_data(auth, m)
            else:
                mp_model = 0 if auth.version == "v1" else 1  # v1 -> 0, v2c -> 1
                auth_data = m.CommunityData(auth.community, mpModel=mp_model)
            error_indication, error_status, _, var_binds = await m.getCmd(
                m.SnmpEngine(),
                auth_data,
                m.UdpTransportTarget((ip_address, auth.port or 161), timeout=timeout, retries=2),
                m.ContextData(),
                m.ObjectType(m.ObjectIdentity(oid)),
            )
            if error_indication or error_status or not var_binds:
                return None
            return _format_snmp_value(var_binds[0][1])

        return asyncio.run(_run())
    except Exception:  # noqa: BLE001
        return None


def _walk_via_pysnmp(ip_address: str, community: str, base_oid: str, version: str, timeout: float, port: int = 161) -> dict[str, str]:
    """Walks a single ifTable column across all interface indexes (v1/v2c
    only -- see _walk_via_pysnmp_v3 for SNMPv3). Returns {index: value}.
    Same tolerant pattern as _get_via_pysnmp -- any failure (unsupported
    OID, timeout, device with no interfaces) returns an empty dict rather
    than raising, so a missing column doesn't fail the poll.
    """
    results: dict[str, str] = {}
    try:
        from pysnmp.hlapi.v1arch.asyncio import (
            CommunityData,
            ObjectIdentity,
            ObjectType,
            SnmpDispatcher,
            UdpTransportTarget,
            next_cmd,
        )

        async def _run() -> None:
            mp_model = 0 if version == "v1" else 1
            with SnmpDispatcher() as dispatcher:
                transport = await UdpTransportTarget.create(
                    (ip_address, port), timeout=timeout, retries=2
                )
                auth = CommunityData(community, mpModel=mp_model)
                var_bind = ObjectType(ObjectIdentity(base_oid))
                while True:
                    error_indication, error_status, _, var_binds = await next_cmd(
                        dispatcher, auth, transport, var_bind,
                    )
                    if error_indication or error_status or not var_binds:
                        break
                    oid, value = var_binds[0]
                    oid_str = str(oid)
                    if not oid_str.startswith(base_oid + "."):
                        break  # walked past the end of this column
                    # Full index suffix (everything after "base_oid."), NOT
                    # just the last OID component. This used to be
                    # `oid_str.rsplit(".", 1)[-1]`, which is correct only
                    # for single-component indices (ifTable's plain
                    # ifIndex) -- every *composite*-index table (ARP's
                    # ifIndex.a.b.c.d, routes' dest.mask.tos.nexthop,
                    # LLDP/CDP's multi-part indices) got silently
                    # truncated down to just its last octet. That's
                    # confirmed against a real device: ARP and routing
                    # discovery came back completely empty (their callers
                    # require >=5 / >=13 index components, so a 1-component
                    # truncated index always got filtered out), while CDP
                    # (2-component) still showed rows but with the wrong
                    # local interface index -- exactly the "CDP half-works,
                    # ARP/routes are empty" symptom this fixes.
                    index = oid_str[len(base_oid) + 1:]
                    results[index] = _format_snmp_value(value)
                    if len(results) >= MAX_INTERFACES_WALKED:
                        break
                    var_bind = ObjectType(ObjectIdentity(oid))

        asyncio.run(_run())
    except ImportError:
        # pysnmp.hlapi.v1arch.asyncio doesn't exist at all in the pinned
        # build (pysnmp==6.2.5 has no `v1arch` submodule -- confirmed via
        # `python3 -c "import pysnmp.hlapi.v1arch.asyncio"` ->
        # ModuleNotFoundError), so this ImportError branch is taken on
        # EVERY call, for every device -- it isn't a rare fallback, it's
        # the only path that ever actually runs.
        #
        # And the fallback itself was silently broken: it did
        # `async for ... in m.nextCmd(...)`, but in this build `nextCmd`
        # is a plain coroutine that returns ONE (errorIndication,
        # errorStatus, errorIndex, varBinds) tuple per call -- the same
        # shape as `getCmd`, which _get_via_pysnmp already awaits
        # directly -- not an async generator. Confirmed directly:
        # `async for x in m.nextCmd(...)` raises `TypeError: 'async for'
        # requires an object with __aiter__ method, got coroutine` before
        # a single SNMP packet is even sent. That TypeError was swallowed
        # by the bare `except Exception` below, so _walk() always
        # returned {} -- which is why CPU/mem/temp/fan/interface stats
        # AND ARP/routing/LLDP/CDP/inventory discovery (everything in
        # this file that walks a table instead of GETting a scalar) came
        # back empty even though the same OIDs read fine with
        # `snmpwalk` directly against the device. Fixed by awaiting
        # nextCmd in a loop, one GETNEXT per iteration, feeding the
        # returned oid back in as the next request's var_bind -- same
        # pattern the v1arch branch above already uses correctly.
        logger.debug("v1arch walk module not available, falling back to v3arch for walk")
        try:
            import pysnmp.hlapi.asyncio as m

            async def _run_v3arch() -> None:
                mp_model = 0 if version == "v1" else 1
                engine = m.SnmpEngine()
                auth_data = m.CommunityData(community, mpModel=mp_model)
                transport = m.UdpTransportTarget((ip_address, port), timeout=timeout, retries=2)
                var_bind = m.ObjectType(m.ObjectIdentity(base_oid))
                while True:
                    error_indication, error_status, _, var_binds = await m.nextCmd(
                        engine, auth_data, transport, m.ContextData(), var_bind, lexicographicMode=False,
                    )
                    if error_indication or error_status or not var_binds:
                        break
                    obj = var_binds[0][0] if isinstance(var_binds[0], list) else var_binds[0]
                    oid, value = obj[0], obj[1]
                    oid_str = str(oid)
                    if not oid_str.startswith(base_oid + "."):
                        break
                    index = oid_str[len(base_oid) + 1:]
                    results[index] = _format_snmp_value(value)
                    if len(results) >= MAX_INTERFACES_WALKED:
                        break
                    var_bind = m.ObjectType(m.ObjectIdentity(oid))

            asyncio.run(_run_v3arch())
        except Exception:
            logger.debug("v3arch walk also failed for %s OID %s", ip_address, base_oid, exc_info=True)
            return results
    except Exception:
        logger.debug("walk failed for %s OID %s", ip_address, base_oid, exc_info=True)
        return results
    return results


def _walk_via_pysnmp_v3(ip_address: str, auth: "SnmpAuthConfig", base_oid: str, timeout: float) -> dict[str, str]:
    """SNMPv3 equivalent of _walk_via_pysnmp. Kept as a separate function
    (rather than branching inside _walk_via_pysnmp) so the proven v1/v2c
    walk path above is untouched -- zero regression risk for the common
    case while v3 gets its own, clearly-labeled best-effort path.

    Uses the same flat `pysnmp.hlapi.asyncio` module as _get_via_pysnmp
    (which already supports UsmUserData for GET) rather than
    `pysnmp.hlapi.v1arch.asyncio` (community-only by design -- "v1arch"
    has no USM support at all, and doesn't exist in this pysnmp build to
    begin with).

    In this build, `nextCmd` is a plain coroutine that returns ONE
    (errorIndication, errorStatus, errorIndex, varBinds) tuple per call --
    the same shape as `getCmd` -- NOT an async generator, despite older
    pysnmp.hlapi docs describing it that way. Confirmed directly:
    `async for x in m.nextCmd(...)` raises `TypeError: 'async for'
    requires an object with __aiter__ method, got coroutine`. This walks
    it correctly instead: one GETNEXT per loop iteration, awaited
    directly, feeding the returned oid back in as the next request's
    var_bind (same pattern as the v1/v2c walk path in _walk_via_pysnmp).
    """
    results: dict[str, str] = {}
    try:
        import pysnmp.hlapi.asyncio as m

        async def _run() -> None:
            engine = m.SnmpEngine()
            auth_data = _build_usm_user_data(auth, m)
            transport = m.UdpTransportTarget((ip_address, auth.port or 161), timeout=timeout, retries=2)
            var_bind = m.ObjectType(m.ObjectIdentity(base_oid))
            while True:
                error_indication, error_status, _, var_binds = await m.nextCmd(
                    engine, auth_data, transport, m.ContextData(), var_bind, lexicographicMode=False,
                )
                if error_indication or error_status or not var_binds:
                    break
                obj = var_binds[0][0] if isinstance(var_binds[0], list) else var_binds[0]
                oid, value = obj[0], obj[1]
                oid_str = str(oid)
                if not oid_str.startswith(base_oid + "."):
                    break
                index = oid_str[len(base_oid) + 1:]
                results[index] = _format_snmp_value(value)
                if len(results) >= MAX_INTERFACES_WALKED:
                    break
                var_bind = m.ObjectType(m.ObjectIdentity(oid))

        asyncio.run(_run())
    except Exception:  # noqa: BLE001
        return results
    return results


def _walk(ip_address: str, auth: "SnmpAuthConfig", base_oid: str, timeout: float) -> dict[str, str]:
    """Version-dispatching walk: v3 goes through USM, v1/v2c through the
    proven community-based path."""
    if auth.version == "v3":
        return _walk_via_pysnmp_v3(ip_address, auth, base_oid, timeout)
    return _walk_via_pysnmp(ip_address, auth.community, base_oid, auth.version, timeout, port=auth.port or 161)


def _get_first_table_value(ip_address: str, auth: "SnmpAuthConfig", base_oid: str, timeout: float) -> str | None:
    """Resolves a single-row-per-device table column (CPU/mem/temp/fan/
    power) without assuming the row lives at instance ".1". Walks the
    column and returns the value of whichever row index the agent
    actually returns first.

    This replaces the old `base_oid + ".1"` GET, which silently returned
    nothing on any device whose row index isn't 1 -- confirmed against a
    real device where cpmCPUTotalTable's only row is index 7, not 1
    (`snmpwalk ... 1.3.6.1.4.1.9.9.109.1.1.1.1.6` -> `...6.7 = Gauge32:
    20`). A GET-based device with a genuinely single-row table only ever
    has one row to walk into anyway, so this is a strict improvement with
    no behavior change for the common case, and fixes the case that broke.
    """
    row = _walk(ip_address, auth, base_oid, timeout)
    if not row:
        return None
    # Rows come back keyed by instance suffix as strings ("1", "7", ...);
    # sort numerically so the lowest real index wins if a device somehow
    # exposes more than one (e.g. multi-CPU chassis), rather than whatever
    # order the dict happens to iterate in.
    try:
        first_index = min(row.keys(), key=lambda idx: int(idx))
    except ValueError:
        first_index = next(iter(row))
    return row[first_index]


def walk_interface_stats(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Walks ifTable/ifXTable and rolls every operationally-up interface
    into fleet-level totals: summed error counters (for the Errors panel)
    and summed HC octets + max link speed (for utilization, computed later
    as a delta against the previous poll -- see metrics_service.compute_interface_utilization).

    Also returns ``per_interface``: the same walk, kept as individual
    per-interface rows (index, descr, octets, speed, errors) rather than
    only the fleet-rolled-up totals above. metrics_service persists these
    to VictoriaMetrics so the Bandwidth Top-N panel can rank
    individual *links* fleet-wide, not just whole devices -- the rollup
    above collapses exactly that per-link detail, which is fine for the
    device-level health score but not enough for a "top 10 congested
    links" view.
    """
    oper_status = _walk(ip_address, auth, IFTABLE_OIDS["ifOperStatus"], timeout)
    if not oper_status:
        return {"errors": None, "octets_total": None, "speed_bps": None, "interface_count": None, "per_interface": []}

    descr = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
    in_errors = _walk(ip_address, auth, IFTABLE_OIDS["ifInErrors"], timeout)
    out_errors = _walk(ip_address, auth, IFTABLE_OIDS["ifOutErrors"], timeout)
    in_octets = _walk(ip_address, auth, IFTABLE_OIDS["ifHCInOctets"], timeout)
    out_octets = _walk(ip_address, auth, IFTABLE_OIDS["ifHCOutOctets"], timeout)
    speed = _walk(ip_address, auth, IFTABLE_OIDS["ifHighSpeed"], timeout)

    total_errors = 0
    total_octets = 0
    total_speed_bps = 0
    up_count = 0
    per_interface: list[dict] = []

    for index, status in oper_status.items():
        is_up = _parse_snmp_enum_int(status) == 1
        if_descr = (descr.get(index) or f"if{index}").strip()

        if not is_up:
            # Down interfaces still get a per_interface entry (so
            # metrics_service can detect the down transition and raise an
            # alert / write interface_statuses history), just excluded
            # from the fleet utilization/error rollups below -- a down
            # port has no meaningful "current" traffic rate. speed_bps is
            # still recorded even though it's down: ifHighSpeed reports
            # the port's configured/negotiated speed regardless of oper
            # status, and metrics_service's SFP-port heuristic (a down
            # port whose speed indicates an optic-capable interface) needs
            # exactly this value on down ports, not just up ones.
            if len(per_interface) < MAX_INTERFACES_WALKED:
                if_speed_bps = int(float(speed.get(index, 0) or 0) * 1_000_000)
                per_interface.append(
                    {
                        "if_index": index,
                        "if_descr": if_descr,
                        "status": "down",
                        "octets_total": None,
                        "speed_bps": if_speed_bps or None,
                        "errors": None,
                    }
                )
            continue

        up_count += 1
        if_errors = int(in_errors.get(index, 0) or 0) + int(out_errors.get(index, 0) or 0)
        if_octets = int(in_octets.get(index, 0) or 0) + int(out_octets.get(index, 0) or 0)
        if_speed_bps = int(float(speed.get(index, 0) or 0) * 1_000_000)  # ifHighSpeed is Mbps per RFC 2863

        total_errors += if_errors
        total_octets += if_octets
        total_speed_bps += if_speed_bps

        if len(per_interface) < MAX_INTERFACES_WALKED:
            per_interface.append(
                {
                    "if_index": index,
                    "if_descr": if_descr,
                    "status": "up",
                    "octets_total": if_octets,
                    "speed_bps": if_speed_bps or None,
                    "errors": if_errors,
                }
            )

    return {
        "errors": total_errors,
        "octets_total": total_octets,
        "speed_bps": total_speed_bps or None,
        "interface_count": up_count,
        "per_interface": per_interface,
    }


def _bitmap_to_port_list(raw: str) -> list[int]:
    """Q-BRIDGE-MIB PortList OCTET STRINGs are a bitmap, one bit per bridge
    port (bit 0 of byte 0 == port 1, MSB-first within each byte). pysnmp
    hands these back either as a real bytes-ish octet string or as a hex
    string like '0xf0 03 ...' / raw escaped bytes depending on agent --
    normalize both before scanning bits."""
    if not raw:
        return []
    cleaned = raw.strip()
    byte_vals: list[int] = []
    hex_tokens = re.findall(r"[0-9A-Fa-f]{2}", cleaned.replace("0x", "").replace(" ", ""))
    if hex_tokens and re.fullmatch(r"(0x)?([0-9A-Fa-f]{2}\s*)+", cleaned):
        byte_vals = [int(h, 16) for h in hex_tokens]
    else:
        byte_vals = [ord(c) for c in cleaned]
    ports = []
    for byte_idx, byte_val in enumerate(byte_vals):
        for bit in range(8):
            if byte_val & (0x80 >> bit):
                ports.append(byte_idx * 8 + bit + 1)
    return ports


def walk_switchport_vlans(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Best-effort switchport (mode / VLAN) lookup via the standard
    BRIDGE-MIB / Q-BRIDGE-MIB, used to enrich the device Interfaces tab
    with Port Mode / VLAN columns (see api.config_management.view_interfaces).

    Walks dot1dBasePortIfIndex to map each bridge port back to its
    ifIndex, then:
      - dot1qPvid for that port's untagged/access (native) VLAN
      - dot1qVlanStaticEgressPorts (per-VLAN egress port bitmap) to see
        how many VLANs each port carries tagged traffic for -- a port
        appearing in more than one VLAN's egress list is a trunk, and
        every VLAN it appears in becomes part of ``trunk_vlans``.

    Returns ``{ifDescr: {"vlan": "<id>", "mode": "access"|"trunk",
    "trunk_vlans": ["10", "20", ...] | None}}`` keyed by the same
    ifDescr string ifTable reports elsewhere in this module.

    Any failure here is swallowed and just leaves these columns
    unpopulated rather than breaking the rest of the interface read --
    not every platform exposes this over SNMP.
    """
    try:
        if_descr = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
        if not if_descr:
            return {}
        base_port_to_if_index = _walk(ip_address, auth, "1.3.6.1.2.1.17.1.4.1.2", timeout)
        pvid_by_base_port = _walk(ip_address, auth, "1.3.6.1.2.1.17.7.1.4.5.1.1", timeout)
        if not base_port_to_if_index or not pvid_by_base_port:
            return {}

        # dot1qVlanStaticEgressPorts: index is the VLAN ID, value is a
        # PortList bitmap of every bridge port that egresses that VLAN
        # (tagged or untagged). A port that shows up under >1 VLAN here is
        # trunking; a port that shows up under exactly the VLAN matching
        # its own PVID (and no others) is a plain access port.
        egress_by_vlan = _walk(ip_address, auth, "1.3.6.1.2.1.17.7.1.4.3.1.2", timeout)
        vlans_by_base_port: dict[str, list[str]] = {}
        for vlan_id, bitmap in (egress_by_vlan or {}).items():
            for port in _bitmap_to_port_list(bitmap):
                vlans_by_base_port.setdefault(str(port), []).append(str(vlan_id))

        result: dict[str, dict] = {}
        for base_port, if_index in base_port_to_if_index.items():
            pvid = pvid_by_base_port.get(base_port)
            descr = if_descr.get(str(if_index))
            if not pvid or not descr:
                continue
            native_vlan = str(_parse_snmp_enum_int(pvid) or pvid)
            member_vlans = sorted(set(vlans_by_base_port.get(str(base_port), [])), key=lambda v: int(v) if v.isdigit() else 0)
            is_trunk = len(member_vlans) > 1
            result[descr.strip()] = {
                "vlan": native_vlan,
                "mode": "trunk" if is_trunk else "access",
                "trunk_vlans": member_vlans if is_trunk else None,
            }
        return result
    except Exception:
        return {}


_DUPLEX_ENUM = {"1": "unknown", "2": "half", "3": "full"}


def walk_interface_duplex(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Best-effort per-port duplex mode via EtherLike-MIB's
    dot3StatsDuplexStatus (1.3.6.1.2.1.10.7.2.1.19), keyed by ifDescr
    like walk_switchport_vlans -- powers duplex-mismatch detection on
    confirmed LLDP/CDP topology links (app.services.topology_service).
    A half/full mismatch between the two ends of a link is a classic
    silent cause of packet loss, retransmits, and "slow but not down"
    complaints that never trips an interface-down alert, so it's worth
    surfacing on the link itself rather than only in raw port config.

    dot3StatsIndex is defined by the MIB as the ifIndex of the
    underlying interface, so -- unlike walk_switchport_vlans, which has
    to translate a separate dot1dBasePortIfIndex mapping first -- this
    can join straight against ifDescr.

    Returns ``{ifDescr: "half" | "full" | "unknown"}`` for every port
    that answered. Ports that don't implement EtherLike-MIB at all
    (non-Ethernet interfaces, some platforms) are simply absent from
    the result, not reported as "unknown" -- that distinction matters
    to the mismatch check, which must never compare against data it
    doesn't actually have. Any failure here is swallowed and just
    leaves duplex unresolved for this device, same posture as every
    other best-effort SNMP enrichment in this module.
    """
    try:
        if_descr = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
        duplex_raw = _walk(ip_address, auth, "1.3.6.1.2.1.10.7.2.1.19", timeout)
        if not if_descr or not duplex_raw:
            return {}
        result: dict[str, str] = {}
        for if_index, raw in duplex_raw.items():
            descr = if_descr.get(str(if_index))
            if not descr:
                continue
            val = str(_parse_snmp_enum_int(raw) if _parse_snmp_enum_int(raw) is not None else raw)
            result[descr.strip()] = _DUPLEX_ENUM.get(val, "unknown")
        return result
    except Exception:
        return {}


def walk_stp_edge_ports(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Best-effort STP edge-port (PortFast) state via Cisco's
    CISCO-STP-EXTENSIONS-MIB (stpxFastPortOperState, .1.3.6.1.4.1.9.9.87.1.4.1.1.2),
    the closest thing to a widely-deployed edge-port indicator over SNMP --
    there's no vendor-neutral MIB for this (RSTP's operEdgePort is only
    exposed via NETCONF/YANG on most platforms).

    Reuses the same dot1dBasePortIfIndex mapping as walk_switchport_vlans
    to translate the MIB's bridge-port index back to ifDescr. Returns
    ``{ifDescr: True|False}`` for ports where a value was read; any
    platform that doesn't implement this MIB (Juniper, most non-Cisco
    gear) or any SNMP failure just yields ``{}``, leaving edge_port
    unpopulated (None) rather than reported as False.
    """
    try:
        if_descr = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
        base_port_to_if_index = _walk(ip_address, auth, "1.3.6.1.2.1.17.1.4.1.2", timeout)
        fast_state = _walk(ip_address, auth, "1.3.6.1.4.1.9.9.87.1.4.1.1.2", timeout)
        if not if_descr or not base_port_to_if_index or not fast_state:
            return {}
        result: dict[str, bool] = {}
        for base_port, if_index in base_port_to_if_index.items():
            descr = if_descr.get(str(if_index))
            state = fast_state.get(str(base_port))
            if not descr or state is None:
                continue
            # stpxFastPortOperState: 1 = enabled (edge/PortFast), 2 = disabled
            result[descr.strip()] = str(_parse_snmp_enum_int(state) or state) == "1"
        return result
    except Exception:
        return {}


def _mac_from_snmp_value(raw: str) -> str:
    """pysnmp returns OctetString MAC values either as a colon-hex string
    already, or as a raw/escaped byte string depending on the agent --
    normalize both into 'aa:bb:cc:dd:ee:ff'."""
    cleaned = raw.strip()
    if re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", cleaned):
        return cleaned.lower()
    # Colon-separated but with un-zero-padded single-digit octets (real
    # devices return e.g. "8:bf:b8:70:9d:fd", not "08:bf:..."). Handle
    # this explicitly and pad each group to 2 digits -- the generic
    # hex-token fallback below can't recover this case: scanning for
    # 2-char hex runs across a colon-delimited string with an odd-length
    # leading octet just drops that octet entirely (it never pairs up
    # with a neighboring hex char before hitting the colon), leaving too
    # few tokens and falling through to returning the raw, unparsed string.
    groups = cleaned.split(":")
    if len(groups) == 6 and all(re.fullmatch(r"[0-9A-Fa-f]{1,2}", g) for g in groups):
        return ":".join(g.zfill(2) for g in groups).lower()
    # Fallback: pull out any hex-looking byte tokens (handles pysnmp's
    # '0xaabbccddeeff' / escaped-octet renderings) and re-join as MAC.
    hex_bytes = re.findall(r"[0-9A-Fa-f]{2}", cleaned.replace("0x", ""))
    if len(hex_bytes) >= 6:
        return ":".join(hex_bytes[-6:]).lower()
    return cleaned


def _discover_arp_table(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """Cisco ARP table (RFC 1213 ipNetToMediaTable). Index suffix is
    'ifIndex.a.b.c.d' -- the destination IP is embedded in the index
    itself, not the value, so it's parsed back out of the suffix."""
    raw = _walk(ip_address, auth, ARP_OIDS["ipNetToMediaPhysAddress"], timeout)
    if not raw:
        # ipNetToMediaTable walks clean but empty on plenty of real
        # devices (IOS-XE routers in particular no longer populate it) --
        # fall back to its RFC 4293 replacement rather than reporting "no
        # ARP entries" when there plainly are some.
        return _discover_arp_table_fallback(ip_address, auth, timeout)
    rows = []
    for index, mac in list(raw.items())[:MAX_DISCOVERY_ROWS]:
        parts = index.split(".")
        if len(parts) < 5:
            continue
        if_index, ip_parts = parts[0], parts[1:5]
        rows.append({
            "if_index": if_index,
            "ip_address": ".".join(ip_parts),
            "mac_address": _mac_from_snmp_value(mac),
        })
    return rows


def _discover_arp_table_fallback(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """RFC 4293 IP-MIB ipNetToPhysicalTable. Index is
    'ifIndex.addrType.addrLen.b1.b2.b3.b4' for an IPv4 (addrType=1)
    entry -- 7 components. IPv6 entries (addrType=2, 16 address bytes)
    are skipped; the Discovery page's ARP tab is IPv4-focused, same as
    the primary ipNetToMediaTable path above.
    """
    raw = _walk(ip_address, auth, ARP_OIDS_FALLBACK["ipNetToPhysicalPhysAddress"], timeout)
    rows = []
    for index, mac in list(raw.items())[:MAX_DISCOVERY_ROWS]:
        parts = index.split(".")
        if len(parts) < 7 or parts[1] != "1":  # not IPv4
            continue
        if_index, ip_parts = parts[0], parts[3:7]
        rows.append({
            "if_index": if_index,
            "ip_address": ".".join(ip_parts),
            "mac_address": _mac_from_snmp_value(mac),
        })
    return rows


def has_default_route(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> bool | None:
    """True if the device's IPv4 routing table contains a default route
    (destination 0.0.0.0), False if the table was read successfully and
    it's genuinely missing, None if the routing table couldn't be read at
    all (SNMP unreachable/unsupported) -- kept distinct from False so
    alert_rule_engine can skip evaluation rather than raising a false
    "route unreachable" alert just because the walk itself failed.
    Backs the ROUTE_UNREACHABLE custom alert-rule metric; a device acting
    as a default gateway itself (destination-only routers with no default
    route by design) shouldn't have this rule scoped to it.
    """
    try:
        routes = _discover_routing_table(ip_address, auth, timeout)
    except Exception:  # noqa: BLE001 - any walk failure reads as "couldn't determine"
        return None
    if not routes:
        return None
    return any(r.get("destination") == "0.0.0.0" for r in routes)


def _discover_routing_table(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """Cisco IPv4 routing table (RFC 1213 ipRouteTable). Index is the
    destination network IP; next-hop/mask/ifIndex are separate walks
    joined on that same index."""
    next_hop = _walk(ip_address, auth, ROUTE_OIDS["ipRouteNextHop"], timeout)
    if not next_hop:
        # Same story as the ARP fallback: ipRouteTable is deprecated and
        # plenty of real devices (IOS-XE in particular) walk it clean but
        # empty. Fall back to IP-FORWARD-MIB's ipCidrRouteTable, which is
        # what those devices actually populate.
        return _discover_routing_table_fallback(ip_address, auth, timeout)
    # mask/if_index are independent walks (both just keyed off the same
    # destination index next_hop already gave us) -- they were previously
    # run one after the other, tripling this job's wall-clock time inside
    # the outer discover_inventory() thread pool (see its docstring: that
    # pool only overlaps the 7 *jobs* with each other, it can't parallelize
    # walks happening serially *within* one job). Run them side by side.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="discover-route") as pool:
        f_mask = pool.submit(_walk, ip_address, auth, ROUTE_OIDS["ipRouteMask"], timeout)
        f_if_index = pool.submit(_walk, ip_address, auth, ROUTE_OIDS["ipRouteIfIndex"], timeout)
        mask = f_mask.result()
        if_index = f_if_index.result()

    rows = []
    for destination, hop in list(next_hop.items())[:MAX_DISCOVERY_ROWS]:
        rows.append({
            "destination": destination,
            "mask": mask.get(destination),
            "next_hop": hop,
            "if_index": if_index.get(destination),
        })
    return rows


def _discover_routing_table_fallback(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """IP-FORWARD-MIB ipCidrRouteTable. Index is the composite
    dest(4).mask(4).tos(1).nexthop(4) -- 13 components -- so destination,
    mask, and next-hop all come straight out of the index; only ifIndex
    needs its own column walk, keyed on that same composite index.
    """
    if_index = _walk(ip_address, auth, ROUTE_OIDS_FALLBACK["ipCidrRouteIfIndex"], timeout)
    rows = []
    for index, ifidx in list(if_index.items())[:MAX_DISCOVERY_ROWS]:
        parts = index.split(".")
        if len(parts) < 13:
            continue
        destination = ".".join(parts[0:4])
        mask = ".".join(parts[4:8])
        next_hop = ".".join(parts[9:13])
        rows.append({
            "destination": destination,
            "mask": mask,
            "next_hop": next_hop,
            "if_index": ifidx,
        })
    return rows


def _format_chassis_id(raw: str | None, subtype: str | None) -> str | None:
    """Best-effort human-readable rendering of lldpRemChassisId. The raw
    walked value is frequently a MAC address already formatted by pysnmp
    (subtype 4 = "macAddress", the common case for a switch/router
    chassis); other subtypes (networkAddress, ifName, local, ...) are
    passed through as-is since there's no single generic way to decode
    them without the ASN.1 type info this SNMP layer already discards.
    """
    if not raw:
        return None
    return raw


def _resolve_lldp_local_port_names(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> dict[str, str]:
    """Maps lldpLocPortNum -> a real, human-readable local interface name
    (e.g. "ge-0/0/0", "GigabitEthernet1/0/1") via lldpLocPortTable.

    lldpLocPortIdSubtype == interfaceName(5) rows are usable as a name
    directly. Junos, however, commonly tags its *local* port ID as
    local(7) rather than interfaceName(5) even though IOS/IOS-XE uses
    interfaceName(5) for the same table -- the same per-vendor split
    _discover_lldp_neighbors already accounts for on the *remote* side
    (see neighbor_port_is_ifindex there). Previously only subtype 5 was
    handled here, so every Junos device's own local ports fell straight
    through to the raw lldpLocPortNum bookkeeping index (an internal LLDP
    table index, not even a real ifIndex) -- e.g. "513", "516" instead of
    "ge-0/0/0". For local(7), lldpLocPortId's value is the device's own
    ifIndex as a numeric string, so it's resolved the same way
    resolve_ifindex_port_name resolves a *neighbor's* local(7) port: an
    ifDescr walk, just against this same device instead of another one.

    Any failure (table not implemented, agent doesn't populate
    lldpLocPortTable) just yields {}, and callers fall back to the raw
    port number.
    """
    try:
        port_ids = _walk(ip_address, auth, LLDP_OIDS["lldpLocPortId"], timeout)
        if not port_ids:
            return {}
        # Now the long pole within the (already-parallel) LLDP job above --
        # subtypes is independent of port_ids, so it doesn't need to wait
        # behind it.
        subtypes = _walk(ip_address, auth, LLDP_OIDS["lldpLocPortIdSubtype"], timeout)
        result: dict[str, str] = {}
        if_descr_cache: dict[str, str] | None = None
        for local_port, port_id in port_ids.items():
            subtype = str(_parse_snmp_enum_int(subtypes.get(local_port)) or subtypes.get(local_port) or "")
            if subtype == LLDP_PORT_SUBTYPE_IFNAME and port_id:
                result[str(local_port)] = str(port_id).strip()
            elif subtype == LLDP_PORT_SUBTYPE_LOCAL and port_id and str(port_id).strip().isdigit():
                if if_descr_cache is None:
                    try:
                        if_descr_cache = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
                    except Exception:
                        if_descr_cache = {}
                resolved = if_descr_cache.get(str(port_id).strip())
                if resolved:
                    result[str(local_port)] = resolved
        return result
    except Exception:
        return {}


def resolve_ifindex_port_name(ip_address: str, auth: "SnmpAuthConfig", if_index: str | None, timeout: float = 3.0) -> str | None:
    """Best-effort ifIndex -> ifDescr lookup against a *specific* device.

    Used to turn a neighbor-reported LLDP port ID that's just a raw
    ifIndex (lldpRemPortIdSubtype == local(7), common on Juniper) into
    a real interface name, by walking ifDescr *on that neighbor device
    itself* (not the device that discovered it) and looking up the
    reported index. Any failure (device unreachable, SNMP not
    configured, index not found) just returns the raw ifIndex string
    unchanged so callers always have something to show.
    """
    if not if_index:
        return if_index
    try:
        if_descr = _walk(ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
        return if_descr.get(str(if_index)) or if_index
    except Exception:
        return if_index


def _discover_lldp_neighbors(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """LLDP-MIB lldpRemTable. Index is 'timeMark.localPortNum.remIndex' --
    the local port number (2nd component) identifies which local
    interface saw the neighbor, but it's an internal LLDP agent handle,
    not a name -- see _resolve_lldp_local_port_names for the
    lldpLocPortTable lookup that turns it into the actual local port
    (e.g. "ge-0/0/0"), which is what the Discovery UI's "Local Port"
    column shows. `local_port_index` is kept alongside as a fallback for
    when that lookup can't resolve a name.

    Anchored on lldpRemChassisId, not lldpRemSysName: System Name is an
    OPTIONAL TLV in LLDP (IEEE 802.1AB), while Chassis ID is mandatory on
    every neighbor entry that exists at all. Cisco IOS happens to send
    the System Name TLV by default, so anchoring on it "worked" for
    Cisco-adjacent neighbors; Junos frequently doesn't populate
    lldpRemSysName even with LLDP fully enabled and neighbors actively
    advertising, which made every Juniper-adjacent walk come back
    completely empty. Falls back to the (always-present) chassis ID for
    `neighbor_name` when the optional system-name TLV wasn't sent.
    """
    chassis_ids = _walk(ip_address, auth, LLDP_OIDS["lldpRemChassisId"], timeout)
    if not chassis_ids:
        return []
    # Four more independent lldpRemTable column walks plus the local-port
    # name resolution (itself 2-3 more walks, see
    # _resolve_lldp_local_port_names) -- this was the single biggest
    # contributor to Discovery's overall latency: even with the outer
    # discover_inventory() pool overlapping all 7 jobs, this one job alone
    # could take 5-6x a single walk's time run serially, making it the
    # long pole the whole request waited on. Overlap them here too.
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="discover-lldp") as pool:
        f_sys_names = pool.submit(_walk, ip_address, auth, LLDP_OIDS["lldpRemSysName"], timeout)
        f_port_ids = pool.submit(_walk, ip_address, auth, LLDP_OIDS["lldpRemPortId"], timeout)
        f_port_id_subtypes = pool.submit(_walk, ip_address, auth, LLDP_OIDS["lldpRemPortIdSubtype"], timeout)
        f_chassis_id_subtypes = pool.submit(_walk, ip_address, auth, LLDP_OIDS["lldpRemChassisIdSubtype"], timeout)
        f_local_port_names = pool.submit(_resolve_lldp_local_port_names, ip_address, auth, timeout)
        sys_names = f_sys_names.result()
        port_ids = f_port_ids.result()
        port_id_subtypes = f_port_id_subtypes.result()
        chassis_id_subtypes = f_chassis_id_subtypes.result()
        local_port_names = f_local_port_names.result()

    rows = []
    for index, chassis_id in list(chassis_ids.items())[:MAX_DISCOVERY_ROWS]:
        parts = index.split(".")
        local_port = parts[1] if len(parts) >= 2 else index
        sys_name = sys_names.get(index)
        raw_neighbor_port = port_ids.get(index)
        neighbor_port_subtype = str(
            _parse_snmp_enum_int(port_id_subtypes.get(index)) or port_id_subtypes.get(index) or ""
        )
        # subtype interfaceName(5): the remote already told us its real
        # interface name, use it as-is. subtype local(7) (Juniper's
        # default): the value is just the *neighbor's own* ifIndex, not
        # human-readable on its own -- flag it so the caller (which has
        # the full device inventory and can look the neighbor up by
        # identity) can resolve it via that neighbor's own ifDescr table.
        # Anything else (macAddress, networkAddress, ...) is left as the
        # raw reported value; there's no generic way to decode it further
        # here.
        neighbor_port_is_ifindex = neighbor_port_subtype == "7" and bool(
            raw_neighbor_port and str(raw_neighbor_port).isdigit()
        )
        rows.append({
            "local_port_index": local_port,
            "local_port": local_port_names.get(local_port) or local_port,
            "neighbor_name": sys_name or _format_chassis_id(chassis_id, chassis_id_subtypes.get(index)),
            "neighbor_port": raw_neighbor_port,
            "neighbor_port_is_ifindex": neighbor_port_is_ifindex,
            "neighbor_chassis_id": chassis_id,
        })
    return rows


def _discover_cdp_neighbors(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """CISCO-CDP-MIB cdpCacheTable. Index is 'ifIndex.deviceIndex' -- the
    local ifIndex (1st component) identifies which local interface saw
    the neighbor. Resolved against ifTable's ifDescr (already walked
    elsewhere in this module for the same device) to turn that ifIndex
    into the real local interface name (e.g. "GigabitEthernet1/0/1")
    instead of leaving the Discovery UI showing a bare numeric index."""
    device_ids = _walk(ip_address, auth, CDP_OIDS["cdpCacheDeviceId"], timeout)
    if not device_ids:
        return []
    # ports/platforms/if_descr are three more independent walks keyed off
    # device_ids' index -- same "job is internally serial" issue as
    # _discover_routing_table above; overlap them instead of paying three
    # full round trips back to back.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="discover-cdp") as pool:
        f_ports = pool.submit(_walk, ip_address, auth, CDP_OIDS["cdpCacheDevicePort"], timeout)
        f_platforms = pool.submit(_walk, ip_address, auth, CDP_OIDS["cdpCachePlatform"], timeout)
        f_if_descr = pool.submit(_walk, ip_address, auth, IFTABLE_OIDS["ifDescr"], timeout)
        ports = f_ports.result()
        platforms = f_platforms.result()
        if_descr = f_if_descr.result() or {}

    rows = []
    for index, neighbor_id in list(device_ids.items())[:MAX_DISCOVERY_ROWS]:
        parts = index.split(".")
        local_if_index = parts[0] if parts else index
        rows.append({
            "local_if_index": local_if_index,
            "local_port": (if_descr.get(local_if_index) or local_if_index).strip()
            if isinstance(if_descr.get(local_if_index), str)
            else local_if_index,
            "neighbor_id": neighbor_id,
            "neighbor_port": ports.get(index),
            "neighbor_platform": platforms.get(index),
        })
    return rows


def _discover_physical_inventory(ip_address: str, auth: "SnmpAuthConfig", timeout: float) -> list[dict]:
    """ENTITY-MIB entPhysicalTable -- chassis, modules, power supplies,
    fans, etc. Index is entPhysicalIndex.

    Walks off entPhysicalDescr now (present on essentially every
    ENTITY-MIB row, including containers) rather than starting from
    entPhysicalSerialNum and requiring a non-empty serial to even be
    considered -- plenty of real devices (virtual/lab platforms
    especially) implement ENTITY-MIB without populating serial numbers
    on most or all rows, which made this come back completely empty even
    though the chassis/module inventory itself was readable. A row with
    no name, no description, and no serial is genuinely empty bookkeeping
    and still gets skipped; anything with at least one of those three is
    now included.
    """
    descrs = _walk(ip_address, auth, INVENTORY_OIDS["entPhysicalDescr"], timeout)
    names = _walk(ip_address, auth, INVENTORY_OIDS["entPhysicalName"], timeout)
    serials = _walk(ip_address, auth, INVENTORY_OIDS["entPhysicalSerialNum"], timeout)
    models = _walk(ip_address, auth, INVENTORY_OIDS["entPhysicalModelName"], timeout)
    classes = _walk(ip_address, auth, INVENTORY_OIDS["entPhysicalClass"], timeout)

    all_indexes = set(descrs) | set(names) | set(serials) | set(models)
    rows = []
    for index in list(all_indexes)[:MAX_DISCOVERY_ROWS]:
        name = names.get(index)
        descr = descrs.get(index)
        serial = serials.get(index)
        if not (name or descr) and not (serial and serial.strip()):
            continue  # nothing usable to show -- purely internal bookkeeping row
        rows.append({
            "index": index,
            "name": name,
            "description": descr,
            "model": models.get(index),
            "serial_number": serial,
            "physical_class": classes.get(index),
        })
    return rows


def _detect_chassis_summary(inventory: list[dict]) -> tuple[str | None, str | None]:
    """Picks (model, serial_number) for the device as a whole out of the
    entPhysicalTable rows discover_inventory() already walked -- that
    table typically has one row per slot/port/PSU/fan *in addition to*
    the chassis itself, each with its own (irrelevant, sub-component)
    model/serial, so naively taking "the first row with a serial" is
    unreliable.

    Prefers the row where entPhysicalClass == PHYSICAL_CLASS_CHASSIS
    (chassis(3), the standard ENTITY-MIB value for exactly this row).
    Falls back to the lowest-index row that has both a model and a
    serial when no row reports class=chassis at all -- some
    lab/virtual platforms (and a handful of older Junos releases)
    implement entPhysicalTable without populating entPhysicalClass
    reliably, and entPhysicalIndex 1 is the chassis by convention on
    essentially every real device.
    """
    chassis_rows = [r for r in inventory if r.get("physical_class") == PHYSICAL_CLASS_CHASSIS]
    candidates = chassis_rows or sorted(
        (r for r in inventory if r.get("model") or r.get("serial_number")),
        key=lambda r: int(r["index"]) if str(r["index"]).isdigit() else 0,
    )
    if not candidates:
        return None, None
    row = candidates[0]
    return row.get("model"), row.get("serial_number")


# Regex-matched against sysDescr to derive a human platform label without
# needing a per-vendor OID -- sysDescr's *format* varies by vendor but is
# always a free-text banner that names the OS somewhere in it, and every
# vendor NetGuard already targets (Cisco IOS/IOS-XE/NX-OS, Juniper Junos,
# Arista EOS) includes one of these tokens near the start of the string.
# Matched in order -- IOS-XE and NX-OS both also contain "Cisco IOS" as a
# substring in some banner formats, so the more specific patterns run
# first.
_PLATFORM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"IOS-XE", re.IGNORECASE), "IOS-XE"),
    (re.compile(r"NX-OS", re.IGNORECASE), "NX-OS"),
    (re.compile(r"\bIOS\b", re.IGNORECASE), "IOS"),
    (re.compile(r"JUNOS", re.IGNORECASE), "Junos"),
    (re.compile(r"\bEOS\b", re.IGNORECASE), "EOS"),
]


def _detect_platform_from_sysdescr(sys_descr: str | None) -> str | None:
    if not sys_descr:
        return None
    for pattern, label in _PLATFORM_PATTERNS:
        if pattern.search(sys_descr):
            return label
    return None


# sysDescr version tokens, matched in order (first match wins). Every
# vendor NetGuard targets embeds the running software version somewhere
# in its sysDescr banner:
#   Cisco IOS/IOS-XE:  "...Version 15.2(4)M6, RELEASE SOFTWARE..."
#   Cisco NX-OS:       "...system: version 9.3(5)..." (lowercase "version")
#   Juniper Junos:      "...JUNOS 20.4R3.8 [...]" or "...kernel JUNOS 20.4R3.8..."
#   Arista EOS:          "...EOS version 4.24.0F..."
# The Cisco/Arista "Version X" pattern is checked first since it's the
# most specific (anchored to the literal word "Version"/"version");
# JUNOS's own pattern only fires when that one didn't match, since Junos
# sysDescr never contains the word "Version" in this sense.
_VERSION_PATTERNS: list[re.Pattern] = [
    re.compile(r"[Vv]ersion\s+([A-Za-z0-9.()/_-]+)"),
    re.compile(r"JUNOS\s+([A-Za-z0-9.-]+)"),
]


def _detect_os_version_from_sysdescr(sys_descr: str | None) -> str | None:
    """Best-effort software version out of sysDescr -- vendor-neutral
    (regex over free text, no per-vendor OID), so it works the same way
    for Cisco and Juniper. Returns None rather than raising when no
    pattern matches (e.g. a vendor NetGuard doesn't specifically target,
    or a sysDescr format we haven't seen) -- an unrecognized banner
    format should leave Device.os_version blank, not crash discovery.
    """
    if not sys_descr:
        return None
    for pattern in _VERSION_PATTERNS:
        match = pattern.search(sys_descr)
        if match:
            # Trim a trailing comma/bracket that sometimes rides along
            # when the version token is immediately followed by more
            # banner text (e.g. "Version 15.2(4)M6," or "JUNOS 20.4R3.8 [").
            return match.group(1).rstrip(",[")
    return None


def discover_inventory(
    ip_address: str, auth: "SnmpAuthConfig", timeout: float = 5.0, vendor: str | None = None
) -> dict:
    """Cisco device discovery: hostname, ARP table, routing table, LLDP/CDP
    neighbors, and chassis/module inventory -- the OIDs from the
    Cisco device polling table (Hostname, ARP Table, Routing Table, LLDP,
    CDP, Inventory). Run on demand (the Discovery action), not on every
    routine health poll, since these are much heavier walks than ifTable
    and the data changes far less often than CPU/memory/interface counters.

    Every sub-walk is independently best-effort: a table the device
    doesn't support (e.g. LLDP disabled, or a non-Cisco device with no
    CDP) just comes back as an empty list rather than failing the whole
    discovery call.

    Also derives device-level `detected_platform` / `detected_model` /
    `detected_serial_number` -- best-effort single values (not a raw
    per-component table like `inventory`) meant to backfill
    Device.platform/model/serial_number on the Overview page when those
    are still blank. See _detect_chassis_summary and
    _detect_platform_from_sysdescr for how each is derived. For Juniper,
    falls back to JUNIPER-MIB jnxBoxDescr/jnxBoxSerialNo (see
    JUNIPER_BOX_OIDS) whenever ENTITY-MIB didn't yield a chassis row --
    common on virtual/lab Junos images, which is why this previously came
    back blank for Juniper specifically while working fine for Cisco.
    """
    # Discovery is on-demand (not the frequent health poll), so it can
    # afford a longer per-request timeout than callers may pass in --
    # floor it rather than trust a short SNMP_TIMEOUT_SECONDS tuned for
    # the routine poll's tighter budget. See DISCOVERY_TIMEOUT_FLOOR.
    timeout = max(timeout, DISCOVERY_TIMEOUT_FLOOR)

    # The 7 walks/GETs below are all independent SNMP round trips against
    # the same device -- none of them reads another's result -- so running
    # them sequentially was pure wasted wall-clock: each one pays its own
    # full request/timeout budget back-to-back, which is exactly why this
    # call took 15-30s end to end (9+ blocking walks in series, then the
    # API layer's switchport walk on top of that). A thread pool overlaps
    # them: total time becomes roughly the slowest single walk instead of
    # the sum of all of them. pysnmp's sync (hlapi) calls release the GIL
    # while blocked on socket I/O, so real threads -- not just asyncio --
    # actually parallelize the network waiting here.
    jobs = {
        "hostname": (_get_via_pysnmp, (ip_address, auth, OIDS["sysName"], timeout)),
        "sys_descr": (_get_via_pysnmp, (ip_address, auth, OIDS["sysDescr"], timeout)),
        "inventory": (_discover_physical_inventory, (ip_address, auth, timeout)),
        "arp_table": (_discover_arp_table, (ip_address, auth, timeout)),
        "routing_table": (_discover_routing_table, (ip_address, auth, timeout)),
        "lldp_neighbors": (_discover_lldp_neighbors, (ip_address, auth, timeout)),
        "cdp_neighbors": (_discover_cdp_neighbors, (ip_address, auth, timeout)),
    }
    results: dict = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="discover") as pool:
        future_to_key = {pool.submit(fn, *args): key for key, (fn, args) in jobs.items()}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception:
                logger.exception("discover_inventory: %s sub-walk failed for %s", key, ip_address)
                # Same best-effort contract as the old sequential version --
                # one failed table shouldn't fail the whole discovery call.
                results[key] = [] if key not in ("hostname", "sys_descr") else None

    hostname = results["hostname"]
    sys_descr = results["sys_descr"]
    inventory = results["inventory"]
    detected_model, detected_serial_number = _detect_chassis_summary(inventory)
    detected_platform = _detect_platform_from_sysdescr(sys_descr)
    detected_os_version = _detect_os_version_from_sysdescr(sys_descr)

    if (vendor or "").lower() == "juniper" and (not detected_model or not detected_serial_number):
        # These two are still independent of each other -- parallelize too
        # instead of adding them back as a serial tail.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="discover-jnx") as pool:
            f_descr = pool.submit(_get_via_pysnmp, ip_address, auth, JUNIPER_BOX_OIDS["jnxBoxDescr"], timeout)
            f_serial = pool.submit(_get_via_pysnmp, ip_address, auth, JUNIPER_BOX_OIDS["jnxBoxSerialNo"], timeout)
            box_descr = f_descr.result()
            box_serial = f_serial.result()
        detected_model = detected_model or box_descr
        detected_serial_number = detected_serial_number or box_serial
        if not detected_platform and box_descr:
            detected_platform = "Junos"

    return {
        "hostname": hostname,
        "arp_table": results["arp_table"],
        "routing_table": results["routing_table"],
        "lldp_neighbors": results["lldp_neighbors"],
        "cdp_neighbors": results["cdp_neighbors"],
        "inventory": inventory,
        "detected_platform": detected_platform,
        "detected_model": detected_model,
        "detected_serial_number": detected_serial_number,
        "detected_os_version": detected_os_version,
    }


def _get_first_table_value_matching_any(
    ip_address: str, auth: "SnmpAuthConfig", descr_oid: str, value_oid: str, keywords: list[str], timeout: float
) -> str | None:
    """Same as _get_first_table_value_matching, but tries several keywords
    in order and returns the first row that matches any of them -- used
    where a single vendor MIB is worded differently across product
    lines. In particular, Juniper's jnxOperatingDescr text for the power
    module is platform-dependent: MX/SRX-class routers commonly say
    "Power Supply 0", but the EX-series switch line (EX3300/EX3400
    included) instead labels it "PEM 0"/"PEM 1" (Power Entry Module).
    Matching on "Power Supply" alone never finds a row on EX hardware,
    so power_supply_status came back "unknown" on every poll for that
    entire product line -- not a connectivity/reachability problem, the
    device answered every SNMP GET correctly, the keyword just never
    matched anything in the table. One shared walk of the description
    column, reused for every keyword tried, keeps this to the same
    number of SNMP round trips as the single-keyword version.
    """
    descr_rows = _walk(ip_address, auth, descr_oid, timeout)
    if not descr_rows:
        return None
    for keyword in keywords:
        match_index = next((idx for idx, descr in descr_rows.items() if keyword.lower() in (descr or "").lower()), None)
        if match_index is not None:
            value_rows = _walk(ip_address, auth, value_oid, timeout)
            return value_rows.get(match_index)
    return None


def _get_first_table_value_matching(
    ip_address: str, auth: "SnmpAuthConfig", descr_oid: str, value_oid: str, keyword: str, timeout: float
) -> str | None:
    """For tables where the row that matters isn't identifiable by index
    alone (e.g. Juniper's jnxOperatingTable holds every hardware
    component -- Routing Engine, PSUs, Fan Trays, PICs -- as rows in the
    *same* table, distinguished only by their jnxOperatingDescr text).
    Walks the description column, finds the first row whose description
    contains `keyword` (case-insensitive), then reads the value column at
    that same row index. Returns None if nothing matches -- e.g. a
    fixed-config Junos box that reports no separate "Fan Tray" component
    correctly reports "no fan telemetry" rather than a wrong value from
    an unrelated row.
    """
    descr_rows = _walk(ip_address, auth, descr_oid, timeout)
    if not descr_rows:
        return None
    match_index = next((idx for idx, descr in descr_rows.items() if keyword.lower() in (descr or "").lower()), None)
    if match_index is None:
        return None
    value_rows = _walk(ip_address, auth, value_oid, timeout)
    return value_rows.get(match_index)


# JUNIPER-MIB jnxOperatingTable (1.3.6.1.4.1.2636.3.1.13.1) -- one row per
# hardware component (Routing Engine, FPCs, PICs, PSUs, Fan Trays), unlike
# Cisco's separate per-purpose tables. jnxOperatingCPU/Buffer/Temp are
# walked and take the first row (typically the Routing Engine, which is
# what "device health" means for CPU/memory/temp); fan/power are matched
# by description text since their state lives in the same jnxOperatingState
# column as every other component.
JUNIPER_OIDS = {
    "descr": "1.3.6.1.4.1.2636.3.1.13.1.5",     # jnxOperatingDescr
    "state": "1.3.6.1.4.1.2636.3.1.13.1.6",     # jnxOperatingState (2=running/ok, 6=down, etc.)
    "temperature": "1.3.6.1.4.1.2636.3.1.13.1.7",  # jnxOperatingTemp (Celsius)
    "cpu": "1.3.6.1.4.1.2636.3.1.13.1.8",       # jnxOperatingCPU (%)
    "buffer": "1.3.6.1.4.1.2636.3.1.13.1.11",   # jnxOperatingBuffer (% -- used as the memory-utilization proxy)
}


def poll_health(
    ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0, vendor: str | None = None
) -> SnmpMetrics:
    """Polls the OIDs in OIDS and returns whatever resolved. Individual
    OID failures don't fail the whole poll (see _get_via_pysnmp); a
    completely unreachable device comes back with reachable=False and an
    error message instead of raising, so the Celery poll task can keep
    going through the rest of the fleet.
    """
    uptime_raw = _get_via_pysnmp(ip_address, auth, OIDS["sysUpTime"], timeout)
    if uptime_raw is None:
        return SnmpMetrics(reachable=False, error="Device did not respond to SNMP GET (sysUpTime)")

    # cpu_5min/mem_used/mem_free/temperature/fan_state/power_supply_state
    # are all table columns, not scalars -- resolved via
    # _get_first_table_value (walk + take whichever row index the agent
    # actually has), not a hardcoded ".1" GET. See OIDS dict comment.
    is_juniper = (vendor or "").lower() == "juniper"

    if is_juniper:
        # jnxOperatingTable (JUNIPER-MIB) holds ONE ROW PER HARDWARE
        # COMPONENT -- chassis, power supplies, fan trays, FPCs/PICs, AND
        # the Routing Engine all share this single table, distinguished
        # only by jnxOperatingDescr. fan_state/power_state below always
        # matched the right row by description text ("Fan" / "Power
        # Supply") -- but cpu/mem/temp used to call
        # _get_first_table_value(), which just walks the column and
        # takes whichever row has the LOWEST numeric index, with no
        # regard for which component that row actually is. On real
        # Juniper hardware (EX3300/EX3400 included) that lowest-indexed
        # row is virtually never the Routing Engine -- it's typically
        # the chassis/backplane or a PSU/fan-tray row, which correctly
        # report 0 for CPU%/buffer%/temperature because those readings
        # don't apply to that component. That's why CPU, memory, and
        # temperature all read 0% together: all three were silently
        # reading the same wrong, non-RE row every poll. Matched by
        # description now, same pattern as fan/power, with a fallback to
        # the old lowest-index behavior only if no row's description
        # actually contains "Routing Engine" (covers oddly-labeled or
        # single-RE-without-that-exact-string platforms rather than
        # coming back with nothing).
        cpu_raw = _get_first_table_value_matching(
            ip_address, auth, JUNIPER_OIDS["descr"], JUNIPER_OIDS["cpu"], "Routing Engine", timeout
        )
        if cpu_raw is None:
            cpu_raw = _get_first_table_value(ip_address, auth, JUNIPER_OIDS["cpu"], timeout)
        # jnxOperatingBuffer is a direct percentage already (unlike Cisco's
        # used/free pool pair) -- no used/free math needed for Juniper.
        mem_used_raw = _get_first_table_value_matching(
            ip_address, auth, JUNIPER_OIDS["descr"], JUNIPER_OIDS["buffer"], "Routing Engine", timeout
        )
        if mem_used_raw is None:
            mem_used_raw = _get_first_table_value(ip_address, auth, JUNIPER_OIDS["buffer"], timeout)
        mem_free_raw = None
        temp_raw = _get_first_table_value_matching(
            ip_address, auth, JUNIPER_OIDS["descr"], JUNIPER_OIDS["temperature"], "Routing Engine", timeout
        )
        if temp_raw is None:
            temp_raw = _get_first_table_value(ip_address, auth, JUNIPER_OIDS["temperature"], timeout)
        fan_state_raw = _get_first_table_value_matching(
            ip_address, auth, JUNIPER_OIDS["descr"], JUNIPER_OIDS["state"], "Fan", timeout
        )
        power_state_raw = _get_first_table_value_matching_any(
            ip_address, auth, JUNIPER_OIDS["descr"], JUNIPER_OIDS["state"],
            ["Power Supply", "PEM", "PSU"], timeout,
        )
    else:
        # cpu_5min/mem_used/mem_free/temperature/fan_state/power_supply_state
        # are all table columns, not scalars -- resolved via
        # _get_first_table_value (walk + take whichever row index the agent
        # actually has), not a hardcoded ".1" GET. See OIDS dict comment.
        cpu_raw = _get_first_table_value(ip_address, auth, OIDS["cpu_5min"], timeout)
        # Fallback: try older/vendor-neutral CPU OIDs if the primary one
        # (CISCO-PROCESS-MIB cpmCPUTotal5minRev) wasn't implemented. These
        # fallbacks are true scalars (OLD-CISCO-CPU-MIB avgBusy5) or already
        # walked with the same row-agnostic helper (legacy cpmCPUTotal5min /
        # HOST-RESOURCES-MIB hrProcessorLoad), so a plain GET is only correct
        # for the genuine scalar; use the table helper for the two row-indexed
        # ones instead of re-adding a hardcoded ".1".
        if cpu_raw is None:
            for fallback_oid in CPU_FALLBACK_OIDS:
                if fallback_oid.endswith(".0"):  # genuine scalar (OLD-CISCO-CPU-MIB avgBusy5)
                    cpu_raw = _get_via_pysnmp(ip_address, auth, fallback_oid, timeout)
                else:  # row-indexed table column -- strip the ".1" and walk instead of assuming index 1
                    base = fallback_oid.rsplit(".", 1)[0] if fallback_oid.endswith(".1") else fallback_oid
                    cpu_raw = _get_first_table_value(ip_address, auth, base, timeout)
                if cpu_raw is not None:
                    logger.debug("CPU fallback OID %s returned %s for %s", fallback_oid, cpu_raw, ip_address)
                    break
        mem_used_raw = _get_first_table_value(ip_address, auth, OIDS["mem_used"], timeout)
        mem_free_raw = _get_first_table_value(ip_address, auth, OIDS["mem_free"], timeout)
        temp_raw = _get_first_table_value(ip_address, auth, OIDS["temperature"], timeout)
        fan_state_raw = _get_first_table_value(ip_address, auth, OIDS["fan_state"], timeout)
        power_state_raw = _get_first_table_value(ip_address, auth, OIDS["power_supply_state"], timeout)
    interface_stats = walk_interface_stats(ip_address, auth, timeout)

    mem_pct = None
    if is_juniper:
        mem_pct = _safe_float(mem_used_raw)  # jnxOperatingBuffer is already a percentage
    elif mem_used_raw is not None and mem_free_raw is not None:
        try:
            used, free = float(mem_used_raw), float(mem_free_raw)
            mem_pct = round((used / (used + free)) * 100, 1) if (used + free) > 0 else None
        except (TypeError, ValueError):
            mem_pct = None

    try:
        uptime_seconds = int(int(uptime_raw) / 100)  # sysUpTime is in TimeTicks (1/100s)
    except (TypeError, ValueError):
        uptime_seconds = None

    return SnmpMetrics(
        cpu_utilization_pct=_safe_float(cpu_raw),
        memory_utilization_pct=mem_pct,
        temperature_celsius=_safe_float(temp_raw),
        uptime_seconds=uptime_seconds,
        fan_status=_juniper_envmon_status(fan_state_raw) if is_juniper else _envmon_status(fan_state_raw),
        power_supply_status=_juniper_envmon_status(power_state_raw) if is_juniper else _envmon_status(power_state_raw),
        reachable=True,
        interface_errors=interface_stats["errors"],
        interface_octets_total=interface_stats["octets_total"],
        interface_speed_bps=interface_stats["speed_bps"],
        interface_count=interface_stats["interface_count"],
        per_interface=interface_stats["per_interface"],
    )


def test_connection(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Lightweight SNMP reachability/credential check: GETs sysDescr and
    sysUpTime only (no CPU/memory/interface table walk) so it's fast
    enough to run synchronously from an API request -- used by
    POST /devices/{id}/snmp-credentials/test to let an operator verify
    community string / SNMPv3 credentials work *before* saving them,
    rather than only finding out on the next scheduled poll.
    """
    sys_descr = _get_via_pysnmp(ip_address, auth, OIDS["sysDescr"], timeout)
    uptime_raw = _get_via_pysnmp(ip_address, auth, OIDS["sysUpTime"], timeout)

    if sys_descr is None and uptime_raw is None:
        return {
            "success": False,
            "message": (
                f"No SNMP response from {ip_address} (version {auth.version}). "
                "Check the IP/port, community string or SNMPv3 credentials, and that the "
                "device's SNMP agent is enabled and reachable from this server."
            ),
            "sys_descr": None,
            "sys_uptime_seconds": None,
        }

    try:
        uptime_seconds = int(int(uptime_raw) / 100) if uptime_raw is not None else None
    except (TypeError, ValueError):
        uptime_seconds = None

    return {
        "success": True,
        "message": f"SNMP {auth.version} connection to {ip_address} succeeded.",
        "sys_descr": sys_descr,
        "sys_uptime_seconds": uptime_seconds,
    }


def _safe_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_snmp_enum_int(raw: str | None) -> int | None:
    """Extracts the numeric code from an SNMP enumerated INTEGER reading.

    pysnmp prints a plain scalar Integer32/Gauge32 (CPU %, memory bytes...)
    as just the number, but for OIDs whose MIB defines named values --
    ifOperStatus, ciscoEnvMonFanState, ciscoEnvMonSupplyState -- it may
    print the symbolic form instead (e.g. "up(1)", "normal(1)") if that
    MIB's textual convention happens to be loaded. A plain `value == "1"`
    string comparison silently breaks in that case (every interface reads
    as down, every fan/PSU reads as unknown) without ever raising an
    error. Handles both "1" and "up(1)" -- and anything else with a
    leading integer -- so status checks work regardless of which form the
    agent/library returns.
    """
    if raw is None:
        return None
    match = re.match(r"\s*(-?\d+)", raw)
    return int(match.group(1)) if match else None


def _envmon_status(raw: str | None) -> str:
    """Maps a ciscoEnvMonFanState/ciscoEnvMonSupplyState reading to the
    fan_status/power_supply_status vocabulary the rest of the app already
    keys off of (see app.core.vm_client, compute_health_score,
    evaluate_thresholds all expect "ok" / "failed" / "unknown" -- "warning"
    is additionally surfaced for the not-yet-critical case rather than
    being silently rounded up to "ok").
    """
    code = _parse_snmp_enum_int(raw)
    if code is None or code in ENVMON_STATE_NOT_PRESENT:
        return "unknown"
    if code in ENVMON_STATE_OK:
        return "ok"
    if code in ENVMON_STATE_WARNING:
        return "warning"
    if code in ENVMON_STATE_FAILED:
        return "failed"
    return "unknown"


# JUNIPER-MIB jnxOperatingState enum (jnxOperatingTable, applies to every
# hardware component type -- not fan/PSU-specific like Cisco's separate
# tables): 1=unknown, 2=running, 3=ready, 4=reset, 5=runningAtFullSpeed,
# 6=down, 7=standby.
_JUNIPER_STATE_OK = {2, 3, 5, 7}
_JUNIPER_STATE_FAILED = {6}


def _juniper_envmon_status(raw: str | None) -> str:
    code = _parse_snmp_enum_int(raw)
    if code is None:
        return "unknown"
    if code in _JUNIPER_STATE_OK:
        return "ok"
    if code in _JUNIPER_STATE_FAILED:
        return "failed"
    return "unknown"


def compute_health_score(metrics: SnmpMetrics) -> tuple[int, str]:
    """Rolls the individual readings into a single 0-100 health score and
    a green/yellow/red classification for the dashboard cards. Missing
    readings are simply excluded from the average rather than treated as
    failures, so a device that only exposes CPU (say) still gets a
    meaningful score instead of defaulting to 0.
    """
    if not metrics.reachable:
        return 0, "red"

    component_scores: list[float] = []

    if metrics.cpu_utilization_pct is not None:
        component_scores.append(max(0.0, 100 - metrics.cpu_utilization_pct))
    if metrics.memory_utilization_pct is not None:
        component_scores.append(max(0.0, 100 - metrics.memory_utilization_pct))
    if metrics.temperature_celsius is not None:
        # 0-40C -> full marks, scales down to 0 by 90C
        temp_score = max(0.0, 100 - max(0.0, metrics.temperature_celsius - 40) * 2)
        component_scores.append(temp_score)
    if metrics.interface_utilization_pct is not None:
        component_scores.append(max(0.0, 100 - metrics.interface_utilization_pct))
    if metrics.fan_status == "failed":
        component_scores.append(0.0)
    if metrics.power_supply_status == "failed":
        component_scores.append(0.0)

    score = round(sum(component_scores) / len(component_scores)) if component_scores else 100
    if score >= 80:
        color = "green"
    elif score >= 50:
        color = "yellow"
    else:
        color = "red"
    return score, color


def evaluate_thresholds(metrics: SnmpMetrics) -> list[tuple[str, str, str]]:
    """Returns (severity, category, message) tuples for any threshold
    breach found in this poll -- fed straight into alert_service to create
    Alert rows, same category vocabulary as inbound SNMP traps.
    """
    findings: list[tuple[str, str, str]] = []

    if not metrics.reachable:
        findings.append(("critical", "Device Unreachable", metrics.error or "SNMP poll failed"))
        return findings

    if metrics.cpu_utilization_pct is not None:
        if metrics.cpu_utilization_pct >= CPU_CRIT_PCT:
            findings.append(("critical", "High CPU", f"CPU utilization at {metrics.cpu_utilization_pct}%"))
        elif metrics.cpu_utilization_pct >= CPU_WARN_PCT:
            findings.append(("warning", "High CPU", f"CPU utilization at {metrics.cpu_utilization_pct}%"))

    if metrics.memory_utilization_pct is not None:
        if metrics.memory_utilization_pct >= MEM_CRIT_PCT:
            findings.append(("critical", "High Memory", f"Memory utilization at {metrics.memory_utilization_pct}%"))
        elif metrics.memory_utilization_pct >= MEM_WARN_PCT:
            findings.append(("warning", "High Memory", f"Memory utilization at {metrics.memory_utilization_pct}%"))

    if metrics.temperature_celsius is not None:
        if metrics.temperature_celsius >= TEMP_CRIT_C:
            findings.append(("critical", "Temperature Critical", f"Chassis temperature at {metrics.temperature_celsius}C"))
        elif metrics.temperature_celsius >= TEMP_WARN_C:
            findings.append(("warning", "Temperature Critical", f"Chassis temperature at {metrics.temperature_celsius}C"))

    if metrics.interface_utilization_pct is not None:
        if metrics.interface_utilization_pct >= IFACE_UTIL_CRIT_PCT:
            findings.append(("critical", "Interface Congestion", f"Interface utilization at {metrics.interface_utilization_pct}%"))
        elif metrics.interface_utilization_pct >= IFACE_UTIL_WARN_PCT:
            findings.append(("warning", "Interface Congestion", f"Interface utilization at {metrics.interface_utilization_pct}%"))

    if metrics.interface_errors is not None and metrics.interface_errors >= IFACE_ERRORS_WARN:
        findings.append(("warning", "Interface Errors", f"{metrics.interface_errors} interface error(s) since last poll"))

    if metrics.fan_status == "failed":
        findings.append(("critical", "Fan Failure", "Fan status reported failed"))
    if metrics.power_supply_status == "failed":
        findings.append(("critical", "Power Failure", "Power supply status reported failed"))

    return findings


# ---------------------------------------------------------------------
# SNMP trap ingestion
# ---------------------------------------------------------------------
KNOWN_TRAP_CATEGORIES = {
    "linkDown": "Interface Down",
    "coldStart": "Device Restart",
    "warmStart": "Device Restart",
    "authenticationFailure": "Authentication Failure",
    "cpmCPURisingThreshold": "High CPU",
    "ciscoEnvMonTemperatureNotification": "Temperature Critical",
    "psFailure": "Power Failure",
}

# Numeric snmpTrapOID.0 value -> the same symbolic names
# KNOWN_TRAP_CATEGORIES/classify_trap already key on. Every inbound trap
# PDU identifies itself only by numeric OID (see trap_service.py's
# receiver callback) -- there's no MIB compiler in the hot path turning
# that into "linkDown" for us, so this reverse map is what actually
# connects a wire-format trap to classify_trap()'s existing severity/
# category logic. IF-MIB/SNMPv2-MIB standard traps are universal across
# every vendor (Cisco/Juniper/Arista/generic Linux net-snmp alike) so
# those are covered unconditionally; linkDown specifically is the one
# this feature is mainly for. The Cisco-specific ones
# (cpmCPURisingThreshold, ciscoEnvMonTemperatureNotification) are
# included since KNOWN_TRAP_CATEGORIES already names them, but psFailure
# is deliberately left unmapped here -- it isn't one universal OID
# across vendors (Cisco splits it per-platform under
# CISCO-ENTITY-*-MIB), so guessing one specific OID for it would be
# more likely wrong than helpful; it still works fine as an unknown trap
# (falls through to Info severity with its raw OID shown) until a real
# fleet's actual OID is added here.
TRAP_OID_NAMES = {
    "1.3.6.1.6.3.1.1.5.1": "coldStart",
    "1.3.6.1.6.3.1.1.5.2": "warmStart",
    "1.3.6.1.6.3.1.1.5.3": "linkDown",
    "1.3.6.1.6.3.1.1.5.4": "linkUp",
    "1.3.6.1.6.3.1.1.5.5": "authenticationFailure",
    "1.3.6.1.4.1.9.9.109.2.0.1": "cpmCPURisingThreshold",  # CISCO-PROCESS-MIB
    "1.3.6.1.4.1.9.9.13.3.0.1": "ciscoEnvMonTemperatureNotification",  # CISCO-ENVMON-MIB
}

# IF-MIB ifIndex -- linkDown/linkUp notifications carry this as one of
# their varbinds (the "affected interface"). Same OID trap_service.py
# uses to pull which port a linkDown/linkUp trap is actually about.
IF_INDEX_VARBIND_OID = "1.3.6.1.2.1.2.2.1.1"


def classify_trap(trap_oid_name: str) -> tuple[str, str]:
    """Maps a trap's varbind name/OID to (severity, category). Unknown
    traps still get recorded as an Info-severity alert rather than
    dropped, so nothing silently disappears just because it isn't in the
    known-traps map yet.
    """
    category = KNOWN_TRAP_CATEGORIES.get(trap_oid_name, trap_oid_name or "Unknown Trap")
    if category in ("Interface Down", "Authentication Failure", "Power Failure", "Temperature Critical"):
        severity = "critical"
    elif category == "High CPU":
        severity = "warning"
    else:
        severity = "info"
    return severity, category
