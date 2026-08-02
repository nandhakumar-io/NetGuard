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
import re
import time
from dataclasses import dataclass

# Standard OIDs used for the health poll. Kept generic (MIB-II + the
# widely-implemented CISCO-PROCESS-MIB/HOST-RESOURCES-MIB equivalents)
# rather than vendor-specific so the same poller works across
# cisco/juniper/arista/linux; a real deployment can extend this map
# per-vendor without changing the calling code.
OIDS = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",  # scalar -- used only by test_connection() below
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
    "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1",  # cpmCPUTotal5minRev.1 (Cisco)
    "mem_used": "1.3.6.1.4.1.9.9.48.1.1.1.5.1",  # ciscoMemoryPoolUsed.1 (pool 1 = Processor)
    "mem_free": "1.3.6.1.4.1.9.9.48.1.1.1.6.1",  # ciscoMemoryPoolFree.1
    "temperature": "1.3.6.1.4.1.9.9.13.1.3.1.3.1",  # ciscoEnvMonTemperatureValue.1
    # CISCO-ENVMON-MIB state tables (also row-indexed, ".1" = first fan
    # tray / first PSU). Replaces the old hardcoded fan_status="ok" /
    # power_supply_status="ok" placeholders with real device telemetry.
    "fan_state": "1.3.6.1.4.1.9.9.13.1.4.1.3.1",  # ciscoEnvMonFanState.1
    "power_supply_state": "1.3.6.1.4.1.9.9.13.1.5.1.3.1",  # ciscoEnvMonSupplyState.1
}

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
    # persisted on DeviceMetric for the next poll to diff against.
    interface_octets_total: int | None = None
    interface_speed_bps: int | None = None
    interface_count: int | None = None


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
                m.UdpTransportTarget((ip_address, auth.port or 161), timeout=timeout, retries=1),
                m.ContextData(),
                m.ObjectType(m.ObjectIdentity(oid)),
            )
            if error_indication or error_status or not var_binds:
                return None
            return str(var_binds[0][1])

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
                    (ip_address, port), timeout=timeout, retries=1
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
                    index = oid_str.rsplit(".", 1)[-1]
                    results[index] = str(value)
                    if len(results) >= MAX_INTERFACES_WALKED:
                        break
                    var_bind = ObjectType(ObjectIdentity(oid))

        asyncio.run(_run())
    except Exception:  # noqa: BLE001
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
    has no USM support at all). Classic pysnmp.hlapi's `nextCmd` walks by
    being an async generator you iterate with `async for`, unlike
    `getCmd`'s single awaitable -- if this build's `nextCmd` turns out not
    to behave that way, this fails closed (returns {} via the except
    below) rather than raising into the poll loop; verify with
    `python3 -c "import pysnmp.hlapi.asyncio as m; help(m.nextCmd)"`
    against your installed build if v3 interface stats stay empty.
    """
    results: dict[str, str] = {}
    try:
        import pysnmp.hlapi.asyncio as m

        async def _run() -> None:
            engine = m.SnmpEngine()
            auth_data = _build_usm_user_data(auth, m)
            transport = m.UdpTransportTarget((ip_address, auth.port or 161), timeout=timeout, retries=1)
            var_bind = m.ObjectType(m.ObjectIdentity(base_oid))
            async for error_indication, error_status, _, var_binds in m.nextCmd(
                engine, auth_data, transport, m.ContextData(), var_bind, lexicographicMode=False,
            ):
                if error_indication or error_status or not var_binds:
                    break
                oid, value = var_binds[0]
                oid_str = str(oid)
                if not oid_str.startswith(base_oid + "."):
                    break
                index = oid_str.rsplit(".", 1)[-1]
                results[index] = str(value)
                if len(results) >= MAX_INTERFACES_WALKED:
                    break

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


def walk_interface_stats(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> dict:
    """Walks ifTable/ifXTable and rolls every operationally-up interface
    into fleet-level totals: summed error counters (for the Errors panel)
    and summed HC octets + max link speed (for utilization, computed later
    as a delta against the previous poll -- see metrics_service.compute_interface_utilization).
    """
    oper_status = _walk(ip_address, auth, IFTABLE_OIDS["ifOperStatus"], timeout)
    if not oper_status:
        return {"errors": None, "octets_total": None, "speed_bps": None, "interface_count": None}

    in_errors = _walk(ip_address, auth, IFTABLE_OIDS["ifInErrors"], timeout)
    out_errors = _walk(ip_address, auth, IFTABLE_OIDS["ifOutErrors"], timeout)
    in_octets = _walk(ip_address, auth, IFTABLE_OIDS["ifHCInOctets"], timeout)
    out_octets = _walk(ip_address, auth, IFTABLE_OIDS["ifHCOutOctets"], timeout)
    speed = _walk(ip_address, auth, IFTABLE_OIDS["ifHighSpeed"], timeout)

    total_errors = 0
    total_octets = 0
    total_speed_bps = 0
    up_count = 0

    for index, status in oper_status.items():
        if _parse_snmp_enum_int(status) != 1:  # not operationally up -- exclude from utilization/error rollup
            continue
        up_count += 1
        total_errors += int(in_errors.get(index, 0) or 0) + int(out_errors.get(index, 0) or 0)
        total_octets += int(in_octets.get(index, 0) or 0) + int(out_octets.get(index, 0) or 0)
        # ifHighSpeed is in Mbps per RFC 2863
        total_speed_bps += int(float(speed.get(index, 0) or 0) * 1_000_000)

    return {
        "errors": total_errors,
        "octets_total": total_octets,
        "speed_bps": total_speed_bps or None,
        "interface_count": up_count,
    }


def poll_health(ip_address: str, auth: "SnmpAuthConfig", timeout: float = 3.0) -> SnmpMetrics:
    """Polls the OIDs in OIDS and returns whatever resolved. Individual
    OID failures don't fail the whole poll (see _get_via_pysnmp); a
    completely unreachable device comes back with reachable=False and an
    error message instead of raising, so the Celery poll task can keep
    going through the rest of the fleet.
    """
    uptime_raw = _get_via_pysnmp(ip_address, auth, OIDS["sysUpTime"], timeout)
    if uptime_raw is None:
        return SnmpMetrics(reachable=False, error="Device did not respond to SNMP GET (sysUpTime)")

    cpu_raw = _get_via_pysnmp(ip_address, auth, OIDS["cpu_5min"], timeout)
    mem_used_raw = _get_via_pysnmp(ip_address, auth, OIDS["mem_used"], timeout)
    mem_free_raw = _get_via_pysnmp(ip_address, auth, OIDS["mem_free"], timeout)
    temp_raw = _get_via_pysnmp(ip_address, auth, OIDS["temperature"], timeout)
    fan_state_raw = _get_via_pysnmp(ip_address, auth, OIDS["fan_state"], timeout)
    power_state_raw = _get_via_pysnmp(ip_address, auth, OIDS["power_supply_state"], timeout)
    interface_stats = walk_interface_stats(ip_address, auth, timeout)

    mem_pct = None
    if mem_used_raw is not None and mem_free_raw is not None:
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
        fan_status=_envmon_status(fan_state_raw),
        power_supply_status=_envmon_status(power_state_raw),
        reachable=True,
        interface_errors=interface_stats["errors"],
        interface_octets_total=interface_stats["octets_total"],
        interface_speed_bps=interface_stats["speed_bps"],
        interface_count=interface_stats["interface_count"],
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
    keys off of (DeviceMetric column comment, compute_health_score,
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