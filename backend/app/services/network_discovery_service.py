"""Network Discovery service: sweeps a CIDR range for live hosts and
writes DiscoveredHost rows for app.api.network_discovery / the Celery
tasks in app.tasks (run_network_discovery_scan_task,
run_discovery_schedule_sweep_task) to read back.

Three pieces:
  parse_and_validate_cidr -- validates/caps a requested range before it
    is ever handed to Celery (see MAX_SCAN_HOSTS).
  run_scan                -- the actual sweep: a single `nmap -sn` host
    discovery pass over the whole CIDR (same proven approach as
    app.services.ipam_service.scan_subnet -- ICMP echo, ARP for
    on-link ranges, and a TCP SYN/ACK fallback for hosts that block
    ping, all in one process rather than one Python thread per host),
    then best-effort reverse DNS + SNMP identification only on the
    hosts nmap actually found alive, then writes one DiscoveredHost per
    responsive IP and updates the DiscoveryScan's summary counters.
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
import re
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.network_discovery import (
    DiscoveredHost,
    DiscoveredHostIpamStatus,
    DiscoveryIgnoreRule,
    DiscoveryScan,
    DiscoveryScanStatus,
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

# `nmap -sn` over a full /22 (1024 addresses) at -T4 comfortably finishes
# well inside this even on a loaded worker; a scan that hasn't returned
# by then is treated the same as a missing/dead nmap binary -- see
# run_scan.
NMAP_SCAN_TIMEOUT_SECONDS = 180

# How often (in seconds, while the nmap host-discovery subprocess is
# running) and in completed enrichment probes (during the per-host
# SNMP/reverse-DNS pass) run_scan re-checks the DB for a CANCELLED
# status -- see run_scan's docstring. Small enough that a cancel takes
# effect within a few seconds even on a full 1024-host sweep, large
# enough not to hammer the DB with a refresh per probe.
CANCEL_CHECK_INTERVAL = 16
CANCEL_POLL_SECONDS = 2.0

# A scan stuck on PENDING/RUNNING this long (worker crashed, the
# "discovery" queue has no consumer, redis lost the task, ...) is
# treated as failed -- same reconciliation pattern as
# app.services.backup_service._reconcile_stuck_jobs. Generous relative
# to nmap's own timeout above: a full 1024-host sweep should finish in
# a couple of minutes at most, so 10 minutes stuck is unambiguously
# "never going to finish", not just slow.
STUCK_SCAN_TIMEOUT_MINUTES = 10


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


def _nmap_discover(db: Session, scan: DiscoveryScan, network: ipaddress.IPv4Network) -> dict[str, dict] | None:
    """Runs `nmap -sn` over the whole CIDR in one subprocess call --
    same proven host-discovery approach as
    app.services.ipam_service.scan_subnet, and a large accuracy/speed
    upgrade over probing every address with a Python TCP-connect
    thread: nmap's ping sweep tries ICMP echo, ARP (for on-link
    ranges, which also yields the MAC address for free), and a TCP
    SYN/ACK fallback for hosts that block ping, in one efficient async
    pass instead of up to MAX_SCAN_HOSTS blocking sockets.

    Returns {ip: {"hostname": str|None, "mac_address": str|None}} for
    every host nmap found alive, or None if the scan was cancelled
    while nmap was still running. Raises RuntimeError if the nmap
    binary isn't installed, or if the scan doesn't finish inside
    NMAP_SCAN_TIMEOUT_SECONDS -- a scan that didn't actually run must
    never be confused with one that ran and found nothing.

    Uses Popen + a poll loop (rather than subprocess.run's blocking
    wait) so a cancel request lands within CANCEL_POLL_SECONDS instead
    of only being noticed after nmap's own timeout -- previously the
    whole discovery step was one call to _tcp_probe per host inside a
    cancel-checked ThreadPoolExecutor loop; a single nmap subprocess
    call needs its own equivalent to keep that same cancel behavior.
    """
    nmap_path = shutil.which("nmap")
    if not nmap_path:
        raise RuntimeError(
            "nmap is not installed in this environment. Install the `nmap` package on the "
            "NetGuard backend host to enable network discovery scans."
        )

    proc = subprocess.Popen(
        [nmap_path, "-sn", "-n", "-T4", str(network)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    elapsed = 0.0
    try:
        while True:
            try:
                stdout, _ = proc.communicate(timeout=CANCEL_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                elapsed += CANCEL_POLL_SECONDS
                if elapsed >= NMAP_SCAN_TIMEOUT_SECONDS:
                    proc.kill()
                    proc.communicate()
                    raise RuntimeError(
                        f"nmap discovery of {network} timed out after {NMAP_SCAN_TIMEOUT_SECONDS}s "
                        "-- try a narrower CIDR."
                    )
                db.refresh(scan, attribute_names=["status"])
                if scan.status == DiscoveryScanStatus.CANCELLED:
                    proc.kill()
                    proc.communicate()
                    return None
    finally:
        if proc.poll() is None:
            proc.kill()

    # Parse nmap's plain-text `-sn` output. Each discovered host is a
    # block starting with either:
    #   "Nmap scan report for 10.0.0.5"                (no rDNS)
    #   "Nmap scan report for host.example.com (10.0.0.5)"  (rDNS resolved)
    # optionally followed by a "MAC Address: AA:BB:.. (Vendor)" line
    # when nmap could ARP for it (on-link ranges, run with raw-socket
    # privileges -- see app.services.ipam_service.fingerprint_subnet's
    # docstring on the capabilities this needs in an unprivileged
    # container).
    hosts: dict[str, dict] = {}
    current_ip: str | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        m = re.match(r"^Nmap scan report for (?:(\S+) )?\(?([\d.]+)\)?\s*$", line)
        if m:
            name, ip = m.group(1), m.group(2)
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                current_ip = None
                continue
            current_ip = ip
            hosts[ip] = {"hostname": name if name and name != ip else None, "mac_address": None}
            continue
        mac_m = re.match(r"^MAC Address:\s*([0-9A-Fa-f:]{17})", line)
        if mac_m and current_ip:
            hosts[current_ip]["mac_address"] = mac_m.group(1).upper()
    return hosts


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


def _enrich_host(ip_address: str, ports: list[int], community: str | None, nmap_hostname: str | None) -> dict:
    """Runs on a worker thread for one host nmap already confirmed is
    alive. Unlike the old per-host liveness probe, this never returns
    None -- the host is already known responsive (that's how it got
    here), so this only fills in the extra detail: which management
    port answered (best-effort; a host can be alive to nmap's ping
    sweep yet have none of `ports` open, e.g. ICMP-only), reverse DNS
    (skipped if nmap's own `-sn` output already resolved a name), and
    SNMP sysName/sysDescr identification.
    """
    open_ports, response_time_ms = _tcp_probe(ip_address, ports)
    hostname = nmap_hostname or _reverse_dns(ip_address)
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

    Two phases, each independently cancel-aware so POST
    /discovery/scans/{id}/cancel (which flips the row to CANCELLED)
    actually stops an in-progress sweep promptly instead of only being
    noticed after the whole thing completes on its own:
      1. `nmap -sn` host discovery over the entire CIDR in one process
         (see _nmap_discover) -- polled every CANCEL_POLL_SECONDS.
      2. Best-effort enrichment (management port, reverse DNS, SNMP
         sysName/sysDescr) of just the hosts nmap found alive, fanned
         out over a small thread pool -- re-checks scan.status every
         CANCEL_CHECK_INTERVAL completed probes, same as before.
    """
    network = parse_and_validate_cidr(scan.cidr)
    ports = [int(p) for p in scan.ports.split(",")] if scan.ports else DEFAULT_PORTS

    all_hosts = [str(ip) for ip in network.hosts()] or [str(network.network_address)]
    scan.total_hosts = len(all_hosts)
    db.commit()

    alive = _nmap_discover(db, scan, network)
    if alive is None:  # cancelled while nmap was still running
        return

    existing_devices = {d.ip_address: d.id for d in db.query(Device.id, Device.ip_address).all() if d.ip_address}

    responsive_count = 0
    new_count = 0
    completed_probes = 0

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(alive)))) as pool:
        futures = {
            pool.submit(_enrich_host, ip, ports, community, info["hostname"]): (ip, info)
            for ip, info in alive.items()
        }
        for future in as_completed(futures):
            completed_probes += 1
            if completed_probes % CANCEL_CHECK_INTERVAL == 0:
                db.refresh(scan, attribute_names=["status"])
                if scan.status == DiscoveryScanStatus.CANCELLED:
                    for f in futures:
                        f.cancel()  # only affects probes not yet started
                    break

            ip_address, nmap_info = futures[future]
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 -- one host's probe thread blowing up shouldn't fail the sweep
                logger.warning("Discovery probe failed for %s", ip_address, exc_info=True)
                continue

            responsive_count += 1
            mac_address = nmap_info["mac_address"]
            vendor_guess = _guess_vendor(result["snmp_sys_descr"], mac_address)
            matched_device_id = existing_devices.get(ip_address)
            ipam_status, ipam_note = _classify_ipam_status(db, ip_address)
            if matched_device_id:
                ipam_status = DiscoveredHostIpamStatus.ASSIGNED

            host = DiscoveredHost(
                scan_id=scan.id,
                ip_address=ip_address,
                ip_sort_key=".".join(octet.zfill(3) for octet in ip_address.split(".")),
                hostname=result["hostname"],
                mac_address=mac_address,
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


def reconcile_stuck_scans(db: Session) -> None:
    """Marks any DiscoveryScan still PENDING/RUNNING long after it should
    have finished (STUCK_SCAN_TIMEOUT_MINUTES) as FAILED -- covers the
    case that made a scan look like it "runs forever": the Celery task
    was never actually picked up (no worker consuming the "discovery"
    queue -- see celery_app.py's task_routes and the `discovery` service
    in docker-compose.yaml) or the worker process died mid-sweep, either
    of which leaves the row on PENDING/RUNNING indefinitely with nothing
    left to ever flip it. Routed to its own "discovery" queue (split out
    from "polling") specifically so a one-off scan can never sit stuck
    behind a continuous stream of recurring SNMP/reachability poll
    tasks on a busy fleet -- that contention was the actual cause of
    "worker never picked it up" in practice, not a missing worker.
    Same fail-safe pattern as
    app.services.backup_service._reconcile_stuck_jobs. Cheap (indexed
    status filter) so it's safe to call on every GET /discovery/scans
    rather than needing a separate sweep task.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_SCAN_TIMEOUT_MINUTES)
    stuck = (
        db.query(DiscoveryScan)
        .filter(DiscoveryScan.status.in_([DiscoveryScanStatus.PENDING, DiscoveryScanStatus.RUNNING]))
        .all()
    )
    changed = False
    for scan in stuck:
        started_at = scan.started_at
        if started_at is None:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if started_at < cutoff:
            scan.status = DiscoveryScanStatus.FAILED
            scan.error = (
                "Scan did not complete in time (worker likely never picked it up, or crashed mid-run). "
                "Check that a Celery worker is consuming the 'discovery' queue."
            )
            scan.completed_at = datetime.now(timezone.utc)
            changed = True
    if changed:
        db.commit()
