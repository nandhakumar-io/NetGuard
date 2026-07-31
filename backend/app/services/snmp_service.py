"""SNMP monitoring service (pysnmp).

Powers the SNMP Health Dashboard: polls a device's standard MIB-II /
vendor CPU-MEM-MIB-style OIDs, turns the raw readings into a 0-100 health
score + green/yellow/red classification, and flags threshold breaches for
the Alert Engine. Also parses inbound SNMP traps (POST /snmp/traps) into
the same category vocabulary the Alert Engine uses for polled breaches, so
"Interface Down" looks the same in the UI whether it came from a trap or
from a poll that noticed the interface was down.
"""
import time
from dataclasses import dataclass

# Standard OIDs used for the health poll. Kept generic (MIB-II + the
# widely-implemented CISCO-PROCESS-MIB/HOST-RESOURCES-MIB equivalents)
# rather than vendor-specific so the same poller works across
# cisco/juniper/arista/linux; a real deployment can extend this map
# per-vendor without changing the calling code.
OIDS = {
    "sysUpTime": "1.3.6.1.2.1.1.3.0",
    "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.8",  # cpmCPUTotal5minRev (Cisco)
    "mem_used": "1.3.6.1.4.1.9.9.48.1.1.1.5",  # ciscoMemoryPoolUsed
    "mem_free": "1.3.6.1.4.1.9.9.48.1.1.1.6",  # ciscoMemoryPoolFree
    "temperature": "1.3.6.1.4.1.9.9.13.1.3.1.3",  # ciscoEnvMonTemperatureValue
}

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


def _get_via_pysnmp(ip_address: str, community: str, oid: str, version: str, timeout: float) -> str | None:
    """Single SNMP GET. Returns None (not an exception) on any failure --
    an unreadable OID is "not applicable"/"unsupported", the same
    tolerant pattern health_monitor.py uses for NAPALM getters, so one
    missing OID doesn't fail the whole poll.
    """
    try:
        from pysnmp.hlapi import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
        )

        mp_model = 0 if version == "v1" else 1  # v1 -> 0, v2c -> 1
        iterator = getCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=mp_model),
            UdpTransportTarget((ip_address, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        error_indication, error_status, _, var_binds = next(iterator)
        if error_indication or error_status or not var_binds:
            return None
        return str(var_binds[0][1])
    except Exception:  # noqa: BLE001
        return None


def _walk_via_pysnmp(ip_address: str, community: str, base_oid: str, version: str, timeout: float) -> dict[str, str]:
    """Walks a single ifTable column across all interface indexes. Returns
    {index: value}. Same tolerant pattern as _get_via_pysnmp -- any failure
    (unsupported OID, timeout, device with no interfaces) returns an empty
    dict rather than raising, so a missing column doesn't fail the poll.
    """
    results: dict[str, str] = {}
    try:
        from pysnmp.hlapi import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            nextCmd,
        )

        mp_model = 0 if version == "v1" else 1
        iterator = nextCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=mp_model),
            UdpTransportTarget((ip_address, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,  # stop once we leave this OID subtree
        )
        for row in iterator:
            error_indication, error_status, _, var_binds = row
            if error_indication or error_status or not var_binds:
                break
            for oid, value in var_binds:
                oid_str = str(oid)
                if not oid_str.startswith(base_oid + "."):
                    return results  # walked past the end of this column
                index = oid_str.rsplit(".", 1)[-1]
                results[index] = str(value)
            if len(results) >= MAX_INTERFACES_WALKED:
                break
    except Exception:  # noqa: BLE001
        return results
    return results


def walk_interface_stats(
    ip_address: str, community: str, version: str = "v2c", timeout: float = 3.0
) -> dict:
    """Walks ifTable/ifXTable and rolls every operationally-up interface
    into fleet-level totals: summed error counters (for the Errors panel)
    and summed HC octets + max link speed (for utilization, computed later
    as a delta against the previous poll -- see metrics_service.compute_interface_utilization).
    """
    oper_status = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifOperStatus"], version, timeout)
    if not oper_status:
        return {"errors": None, "octets_total": None, "speed_bps": None, "interface_count": None}

    in_errors = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifInErrors"], version, timeout)
    out_errors = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifOutErrors"], version, timeout)
    in_octets = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifHCInOctets"], version, timeout)
    out_octets = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifHCOutOctets"], version, timeout)
    speed = _walk_via_pysnmp(ip_address, community, IFTABLE_OIDS["ifHighSpeed"], version, timeout)

    total_errors = 0
    total_octets = 0
    total_speed_bps = 0
    up_count = 0

    for index, status in oper_status.items():
        if status != "1":  # not operationally up -- exclude from utilization/error rollup
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


def poll_health(
    ip_address: str,
    community: str,
    version: str = "v2c",
    timeout: float = 3.0,
) -> SnmpMetrics:
    """Polls the OIDs in OIDS and returns whatever resolved. Individual
    OID failures don't fail the whole poll (see _get_via_pysnmp); a
    completely unreachable device comes back with reachable=False and an
    error message instead of raising, so the Celery poll task can keep
    going through the rest of the fleet.
    """
    uptime_raw = _get_via_pysnmp(ip_address, community, OIDS["sysUpTime"], version, timeout)
    if uptime_raw is None:
        return SnmpMetrics(reachable=False, error="Device did not respond to SNMP GET (sysUpTime)")

    cpu_raw = _get_via_pysnmp(ip_address, community, OIDS["cpu_5min"], version, timeout)
    mem_used_raw = _get_via_pysnmp(ip_address, community, OIDS["mem_used"], version, timeout)
    mem_free_raw = _get_via_pysnmp(ip_address, community, OIDS["mem_free"], version, timeout)
    temp_raw = _get_via_pysnmp(ip_address, community, OIDS["temperature"], version, timeout)
    interface_stats = walk_interface_stats(ip_address, community, version, timeout)

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
        fan_status="ok",  # placeholder pending per-vendor fan OID mapping
        power_supply_status="ok",  # placeholder pending per-vendor PSU OID mapping
        reachable=True,
        interface_errors=interface_stats["errors"],
        interface_octets_total=interface_stats["octets_total"],
        interface_speed_bps=interface_stats["speed_bps"],
        interface_count=interface_stats["interface_count"],
    )


def _safe_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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