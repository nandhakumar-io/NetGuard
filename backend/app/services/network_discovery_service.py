"""Network Discovery service: sweeps a CIDR range for live hosts and
writes DiscoveredHost rows for app.api.network_discovery / the Celery
tasks in app.tasks (run_network_discovery_scan_task,
run_discovery_schedule_sweep_task) to read back.

Three pieces:
  parse_and_validate_cidr -- validates/caps a requested range before it
    is ever handed to Celery (see MAX_SCAN_HOSTS).
  run_scan                -- the actual sweep: TCP-connect probe every
    host in the range, best-effort reverse DNS + SNMP identification on
    anything that answers, then writes one DiscoveredHost per responsive
    IP and updates the DiscoveryScan's summary counters.
  _guess_vendor / _classify_ipam_status -- per-host enrichment helpers
    used while writing rows.

Deliberately degrades a lot: any single host's probe/DNS/SNMP failure is
caught and logged, never allowed to abort the whole sweep (see
app.tasks.run_network_discovery_scan_task's docstring, which relies on
that -- it only wraps run_scan itself in a try/except, for genuinely
unexpected failures like a DB error).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.network_discovery import (
    DiscoveredHost,
    DiscoveredHostIpamStatus,
    DiscoveryIgnoreRule,
    DiscoveryScan,
)
from app.models.subnet import IPAddressState, IPReservation, Subnet
from app.services import oui_lookup, snmp_service

logger = logging.getLogger("netguard.network_discovery")

# Caps how large a single scan's range can be -- an unbounded /8 typed
# into the CIDR box would otherwise queue millions of probes on one
# Celery worker. 1024 hosts (a /22) is generous for a single sweep while
# still finishing in a reasonable window; larger ranges should be split
# into multiple scans/schedules by the operator.
MAX_SCAN_HOSTS = 1024

DEFAULT_PORTS = [22, 23, 80, 443, 161, 3389]

TCP_PROBE_TIMEOUT_SECONDS = 0.75
SNMP_TIMEOUT_SECONDS = 1.5
MAX_WORKERS = 64


def parse_and_validate_cidr(cidr: str) -> ipaddress.IPv4Network:
    """Parses and size-caps a CIDR string. Raises ValueError (caught by
    the API layer as a 422) on anything malformed or too large to scan.
    """
    try:
        network = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"'{cidr}' is not a valid CIDR range: {exc}") from exc

    if network.version != 4:
        raise ValueError("Only IPv4 ranges are supported for network discovery.")

    if network.num_addresses > MAX_SCAN_HOSTS:
        raise ValueError(
            f"{cidr} covers {network.num_addresses} addresses, more than the "
            f"{MAX_SCAN_HOSTS}-host limit for a single scan. Use a smaller range."
        )

    return network


def _tcp_probe(ip_address: str, ports: list[int]) -> tuple[list[int], float | None]:
    """Tries every port in `ports` against `ip_address`, stopping at the
    first that accepts a connection. Returns (open_ports_found, elapsed_ms)
    -- open_ports_found is just the one port that answered first (cheap
    "is this host alive" check, not a full port scan), elapsed_ms is None
    if nothing answered.
    """
    for port in ports:
        started = datetime.now(timezone.utc)
        try:
            with socket.create_connection((ip_address, port), timeout=TCP_PROBE_TIMEOUT_SECONDS):
                elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                return [port], round(elapsed_ms, 1)
        except OSError:
            continue
    return [], None


def _reverse_dns(ip_address: str) -> str | None:
    try:
        socket.setdefaulttimeout(TCP_PROBE_TIMEOUT_SECONDS)
        return socket.gethostbyaddr(ip_address)[0]
    except (socket.herror, socket.gaierror, socket.timeout, OSError):
        return None
    finally:
        socket.setdefaulttimeout(None)


def _snmp_identify(ip_address: str, community: str | None) -> tuple[str | None, str | None]:
    """Best-effort (sys_name, sys_descr) via SNMP v2c GET. Returns
    (None, None) when no community was supplied or the device doesn't
    answer -- never raises."""
    if not community:
        return None, None
    auth = snmp_service.SnmpAuthConfig(version="v2c", community=community)
    try:
        sys_name = snmp_service._get_via_pysnmp(ip_address, auth, snmp_service.OIDS["sysName"], SNMP_TIMEOUT_SECONDS)
        sys_descr = snmp_service._get_via_pysnmp(
            ip_address, auth, snmp_service.OIDS["sysDescr"], SNMP_TIMEOUT_SECONDS
        )
        return sys_name, sys_descr
    except Exception:  # noqa: BLE001 -- one host's SNMP hiccup shouldn't fail the scan
        return None, None


# sysDescr keyword -> vendor guess. Deliberately wider than DeviceVendor
# (see app.api.network_discovery._resolve_vendor's docstring for why
# those two lists don't match 1:1) -- this is meant to be the most
# specific free-text guess available, narrowed to NetGuard's actual enum
# only at import time.
_SYSDESCR_VENDOR_KEYWORDS: list[tuple[str, str]] = [
    ("cisco", "cisco"),
    ("ios-xe", "cisco"),
    ("nx-os", "cisco"),
    ("juniper", "juniper"),
    ("junos", "juniper"),
    ("arista", "arista"),
    ("eos version", "arista"),
    ("fortinet", "fortinet"),
    ("fortigate", "fortinet"),
    ("aruba", "aruba"),
    ("mikrotik", "mikrotik"),
    ("routeros", "mikrotik"),
    ("hp procurve", "hp"),
    ("hewlett packard", "hp"),
    ("linux", "linux"),
    ("ubuntu", "linux"),
]


def _guess_vendor(sys_descr: str | None, mac_address: str | None) -> str | None:
    """Best-effort vendor guess for a discovered host: sysDescr keyword
    match first (most specific, requires SNMP to have answered), falling
    back to OUI lookup on the MAC address (works even with no SNMP
    community configured). Returns None -- never a fabricated guess --
    when neither source resolves anything.
    """
    if sys_descr:
        lowered = sys_descr.lower()
        for keyword, vendor in _SYSDESCR_VENDOR_KEYWORDS:
            if keyword in lowered:
                return vendor
    oui_vendor = oui_lookup.lookup_oui(mac_address)
    if oui_vendor:
        return oui_vendor.lower()
    return None


def _classify_ipam_status(db: Session, ip_address: str) -> tuple[DiscoveredHostIpamStatus, str | None]:
    """Cross-references one discovered IP against IPAM state -- see
    DiscoveredHostIpamStatus for what each outcome means. Checked in
    order: already a managed Device (ASSIGNED) > held by a reservation
    (EXPECTED) > covered by a managed subnet with no reservation
    (ROGUE) > not covered by any managed subnet at all (UNMANAGED).
    """
    if db.query(Device.id).filter(Device.ip_address == ip_address).first():
        return DiscoveredHostIpamStatus.ASSIGNED, None

    try:
        addr = ipaddress.ip_address(ip_address)
    except ValueError:
        return DiscoveredHostIpamStatus.UNMANAGED, None

    for subnet in db.query(Subnet).all():
        try:
            net = ipaddress.ip_network(subnet.cidr, strict=False)
        except ValueError:
            continue
        if addr not in net:
            continue
        reservation = (
            db.query(IPReservation)
            .filter(
                IPReservation.subnet_id == subnet.id,
                IPReservation.ip_address == ip_address,
                IPReservation.state == IPAddressState.RESERVED,
            )
            .first()
        )
        if reservation:
            return DiscoveredHostIpamStatus.EXPECTED, reservation.note
        return DiscoveredHostIpamStatus.ROGUE, None

    return DiscoveredHostIpamStatus.UNMANAGED, None


def _ignore_rule_for(db: Session, schedule_id, ip_address: str, vendor_guess: str | None) -> DiscoveryIgnoreRule | None:
    if not schedule_id:
        return None
    return (
        db.query(DiscoveryIgnoreRule)
        .filter(
            DiscoveryIgnoreRule.schedule_id == schedule_id,
            DiscoveryIgnoreRule.ip_address == ip_address,
            DiscoveryIgnoreRule.vendor_guess == vendor_guess,
        )
        .first()
    )


def _probe_host(ip_address: str, ports: list[int], community: str | None) -> dict | None:
    """Runs on a worker thread for one host. Returns None for a
    non-responsive host (nothing is written for it -- DiscoveredHost
    only tracks hosts that actually answered), or a dict of raw findings
    for a responsive one.
    """
    open_ports, response_time_ms = _tcp_probe(ip_address, ports)
    if not open_ports:
        return None

    hostname = _reverse_dns(ip_address)
    sys_name, sys_descr = _snmp_identify(ip_address, community)
    return {
        "ip_address": ip_address,
        "open_ports": open_ports,
        "response_time_ms": response_time_ms,
        "hostname": hostname,
        "snmp_sys_name": sys_name,
        "snmp_sys_descr": sys_descr,
    }


def run_scan(db: Session, scan: DiscoveryScan, community: str | None = None) -> None:
    """Sweeps `scan.cidr`, writing one DiscoveredHost per responsive IP
    and updating scan.total_hosts/responsive_hosts/new_hosts in place.
    Does not set scan.status/completed_at -- the caller (see
    app.tasks.run_network_discovery_scan_task /
    run_discovery_schedule_sweep_task) owns that so it can distinguish
    "run_scan raised" from "run_scan finished normally".

    `community` is the plaintext SNMP community (already decrypted by
    the caller from scan.snmp_community_ref, if one was supplied) --
    never re-read from the DB here so this function has no crypto
    dependency of its own beyond what's already resolved for it.
    """
    network = parse_and_validate_cidr(scan.cidr)
    ports = [int(p) for p in scan.ports.split(",")] if scan.ports else DEFAULT_PORTS

    hosts = [str(ip) for ip in network.hosts()] or [str(network.network_address)]
    scan.total_hosts = len(hosts)

    existing_devices = {d.ip_address: d.id for d in db.query(Device.id, Device.ip_address).all() if d.ip_address}

    responsive_count = 0
    new_count = 0

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(hosts)))) as pool:
        futures = {pool.submit(_probe_host, ip, ports, community): ip for ip in hosts}
        for future in as_completed(futures):
            ip_address = futures[future]
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 -- one host's probe thread blowing up shouldn't fail the sweep
                logger.warning("Discovery probe failed for %s", ip_address, exc_info=True)
                continue
            if result is None:
                continue

            responsive_count += 1
            vendor_guess = _guess_vendor(result["snmp_sys_descr"], None)
            matched_device_id = existing_devices.get(ip_address)
            ipam_status, ipam_note = _classify_ipam_status(db, ip_address)
            if matched_device_id:
                ipam_status = DiscoveredHostIpamStatus.ASSIGNED

            host = DiscoveredHost(
                scan_id=scan.id,
                ip_address=ip_address,
                ip_sort_key=".".join(octet.zfill(3) for octet in ip_address.split(".")),
                hostname=result["hostname"],
                mac_address=None,
                open_ports=",".join(str(p) for p in result["open_ports"]),
                snmp_sys_name=result["snmp_sys_name"],
                snmp_sys_descr=result["snmp_sys_descr"],
                vendor_guess=vendor_guess,
                response_time_ms=result["response_time_ms"],
                matched_device_id=matched_device_id,
                ipam_status=ipam_status,
                ipam_reservation_note=ipam_note,
            )

            if not matched_device_id:
                rule = _ignore_rule_for(db, scan.schedule_id, ip_address, vendor_guess)
                if rule:
                    host.ignored = True
                    host.ignored_by = rule.ignored_by
                    host.ignored_at = rule.ignored_at
                else:
                    new_count += 1

            db.add(host)

    scan.responsive_hosts = responsive_count
    scan.new_hosts = new_count
    db.commit()
