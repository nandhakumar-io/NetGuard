"""Network Discovery: sweep a CIDR range for live hosts and best-effort
identify them, so an operator can pull newly-seen devices into inventory
without hand-typing every IP.

Deliberately does NOT use raw ICMP (no CAP_NET_RAW in the app/worker
containers, and requiring it just for a "is anything there" probe is a
bigger deployment ask than this feature is worth) -- "alive" is decided
by a concurrent TCP connect probe across a small set of commonly-open
management ports (22 SSH, 23 Telnet, 80/443 HTTP(S), 161 SNMP, 3389 RDP).
A host that answers on *any* of those, even with a connection refused
(RST) rather than a full handshake, is reachable -- a closed port still
proves something is listening on that IP, whereas a connect *timeout*
means nothing answered. This is the same category of technique
`nmap -PS`/`-sT` uses for firewalled hosts that drop ICMP.

Responsive hosts get two more best-effort enrichment passes, both
independently swallowed on failure since neither should stop the scan:
  1. Reverse DNS (socket.gethostbyaddr) for a hostname.
  2. SNMP sysName/sysDescr via app.services.snmp_service, only when the
     caller supplied a community string -- most freshly-racked gear
     still has default-deny or no community configured, so this is
     opportunistic, not required for a host to show up in results.
"""
import concurrent.futures
import ipaddress
import logging
import socket
import time

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.network_discovery import (
    DiscoveredHost,
    DiscoveredHostIpamStatus,
    DiscoveryScan,
)
from app.models.subnet import IPAddressState, IPReservation
from app.services import ipam_service, oui_lookup

logger = logging.getLogger(__name__)

# Hard cap on range size -- a /16 (65k hosts) fanned out through this
# thread-pool sweep would take forever and hammer the local network; an
# operator who genuinely needs to sweep something that big should scope
# it into several smaller scans instead. /22 (1024 hosts) is generous
# for a single site/VLAN sweep and finishes in well under a minute.
MAX_SCAN_HOSTS = 1024

DEFAULT_PORTS = (22, 23, 80, 443, 161, 3389)
CONNECT_TIMEOUT_SECONDS = 0.75
MAX_WORKERS = 64


def parse_and_validate_cidr(cidr: str) -> ipaddress.IPv4Network:
    """Raises ValueError with a caller-safe message on anything invalid
    or too large -- shared by the API layer (fail fast, 422, before ever
    touching Celery) and the task itself (defense in depth against a row
    that was somehow written with a bad value)."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ValueError(f"'{cidr}' is not a valid CIDR range: {exc}") from exc

    if network.version != 4:
        raise ValueError("Only IPv4 ranges are supported")

    host_count = network.num_addresses - 2 if network.num_addresses > 2 else network.num_addresses
    if host_count > MAX_SCAN_HOSTS:
        raise ValueError(
            f"Range too large ({host_count} hosts) -- scan at most a /22 "
            f"({MAX_SCAN_HOSTS} hosts) at a time. Split larger ranges into "
            "multiple scans."
        )
    return network


def _host_list(network: ipaddress.IPv4Network) -> list[str]:
    """Usable host IPs in the range. For anything /31 or smaller (point-
    to-point / single host, no distinct network/broadcast address),
    every address in the range is a candidate host."""
    if network.prefixlen >= 31:
        return [str(ip) for ip in network]
    return [str(ip) for ip in network.hosts()]


def _sort_key(ip_address: str) -> str:
    return ".".join(part.zfill(3) for part in ip_address.split("."))


def _probe_host(ip_address: str, ports: tuple[int, ...]) -> tuple[list[int], float | None]:
    """Tries each port in turn, stopping at the first that answers.
    Returns (open_ports, response_time_ms) -- open_ports has at most the
    one port that answered (further ports aren't worth the extra time
    once a host is known to be alive), response_time_ms is None if
    nothing answered on any probed port."""
    for port in ports:
        start = time.monotonic()
        try:
            with socket.create_connection((ip_address, port), timeout=CONNECT_TIMEOUT_SECONDS):
                elapsed_ms = (time.monotonic() - start) * 1000
                return [port], round(elapsed_ms, 1)
        except (ConnectionRefusedError, OSError) as exc:
            # A refused connection (RST) still proves the host is up --
            # only ConnectionRefusedError specifically means "something
            # answered and said no"; other OSErrors here are almost
            # always timeout/unreachable and mean try the next port.
            if isinstance(exc, ConnectionRefusedError):
                elapsed_ms = (time.monotonic() - start) * 1000
                return [port], round(elapsed_ms, 1)
            continue
    return [], None


def _reverse_dns(ip_address: str) -> str | None:
    try:
        socket.setdefaulttimeout(1.0)
        hostname, _, _ = socket.gethostbyaddr(ip_address)
        return hostname
    except (socket.herror, socket.gaierror, socket.timeout, OSError):
        return None
    finally:
        socket.setdefaulttimeout(None)


def _read_local_arp_table() -> dict[str, str]:
    """Best-effort {ip_address: mac_address} from the host/container's own
    ARP/neighbor cache (/proc/net/arp on Linux). Only ever has entries for
    IPs on a directly-attached subnet that this container has actually
    talked to (a TCP probe populates it, which is why this is read *after*
    the probe sweep, not before) -- a container without host networking
    generally can't see the physical LAN's ARP table at all, in which case
    this just returns an empty dict and every host's mac_address/OUI
    vendor guess stays None. That's a deployment-topology limitation, not
    a bug: reading an ARP table this way is fundamentally local-subnet-
    only, unlike the TCP-probe sweep or SNMP identification, neither of
    which cares whether the target is L2-adjacent.
    """
    table: dict[str, str] = {}
    try:
        with open("/proc/net/arp") as fh:
            lines = fh.readlines()[1:]  # header row
    except OSError:
        return table

    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip_address, _hw_type, flags, mac = parts[0], parts[1], parts[2], parts[3]
        # flags == 0x0 means an incomplete/stale entry with no real MAC
        # (still shows as 00:00:00:00:00:00) -- skip those.
        if flags == "0x0" or mac in ("00:00:00:00:00:00", "<incomplete>"):
            continue
        table[ip_address] = mac
    return table


def _guess_vendor(sys_descr: str | None) -> str | None:
    if not sys_descr:
        return None
    text = sys_descr.lower()
    for needle, vendor in (
        ("cisco", "Cisco"), ("junos", "Juniper"), ("juniper", "Juniper"),
        ("arista", "Arista"), ("linux", "Linux"), ("mikrotik", "MikroTik"),
        ("fortinet", "Fortinet"), ("hp ", "HP"), ("aruba", "Aruba"),
    ):
        if needle in text:
            return vendor
    return None


def _snmp_identify(ip_address: str, community: str | None) -> tuple[str | None, str | None]:
    if not community:
        return None, None
    from app.services.snmp_service import SnmpAuthConfig, _get_via_pysnmp

    auth = SnmpAuthConfig(version="v2c", community=community)
    try:
        sys_name = _get_via_pysnmp(ip_address, auth, "1.3.6.1.2.1.1.5.0", timeout=1.5)
        sys_descr = _get_via_pysnmp(ip_address, auth, "1.3.6.1.2.1.1.1.0", timeout=1.5)
        return sys_name, sys_descr
    except Exception:
        logger.debug("SNMP identification failed for %s", ip_address, exc_info=True)
        return None, None


def _probe_and_enrich(ip_address: str, ports: tuple[int, ...], community: str | None) -> dict | None:
    open_ports, response_ms = _probe_host(ip_address, ports)
    if not open_ports:
        return None

    hostname = _reverse_dns(ip_address)
    sys_name, sys_descr = _snmp_identify(ip_address, community)
    return {
        "ip_address": ip_address,
        "open_ports": ",".join(str(p) for p in open_ports),
        "response_time_ms": response_ms,
        "hostname": hostname,
        "snmp_sys_name": sys_name,
        "snmp_sys_descr": sys_descr,
        "vendor_guess": _guess_vendor(sys_descr),
    }


def _classify_ipam_status(
    db: Session, ip_address: str, matched_device_id
) -> tuple[DiscoveredHostIpamStatus, str | None]:
    """Cross-references a responsive IP against IPAM (app.services.ipam_service)
    to tell "expected but not yet provisioned" (IPAM already holds this
    address with a RESERVED IPReservation) apart from "rogue/unexpected"
    (a managed Subnet covers this address and has no reservation for it,
    and it's not an already-known Device either). See
    DiscoveredHostIpamStatus's docstring for the full breakdown -- this
    is deliberately conservative: an address is only ever called ROGUE
    when IPAM actually manages the covering subnet, since "nobody
    entered this /24 into IPAM yet" and "something showed up that
    shouldn't be here" are very different operational situations and
    conflating them would train operators to ignore the rogue flag.
    """
    if matched_device_id:
        return DiscoveredHostIpamStatus.ASSIGNED, None

    subnet = ipam_service.find_subnet_for_ip(db, ip_address)
    if subnet is None:
        return DiscoveredHostIpamStatus.UNMANAGED, None

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


def run_scan(db: Session, scan: DiscoveryScan, community: str | None) -> None:
    """Sweeps scan.cidr, writing one DiscoveredHost row per responsive
    IP and updating scan's status/counters. Never raises out to the
    caller (Celery task) on a per-host probe failure -- only a CIDR that
    somehow got past validation, or a DB error, aborts the whole scan.
    Matches every other best-effort background sweep in this codebase
    (drift, reachability, SNMP polling): partial results beat none.
    """
    ports = DEFAULT_PORTS
    if scan.ports:
        try:
            ports = tuple(int(p.strip()) for p in scan.ports.split(",") if p.strip())
        except ValueError:
            pass

    network = parse_and_validate_cidr(scan.cidr)
    hosts = _host_list(network)
    scan.total_hosts = len(hosts)
    db.add(scan)
    db.commit()

    existing_devices = {d.ip_address: d.id for d in db.query(Device.id, Device.ip_address).all()}

    responsive = 0
    new_count = 0
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_probe_and_enrich, ip, ports, community): ip for ip in hosts}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception:
                logger.warning("Discovery probe crashed for %s", futures[future], exc_info=True)
                continue
            if result:
                results.append(result)

    # Read the local ARP/neighbor cache once, after every probe has had a
    # chance to populate it (a fresh TCP connect to a directly-attached
    # host is usually what causes the kernel to ARP for it in the first
    # place) -- see _read_local_arp_table's docstring for why this only
    # ever covers L2-adjacent hosts.
    arp_table = _read_local_arp_table()

    for result in results:
        responsive += 1
        ip_address = result["ip_address"]
        matched_device_id = existing_devices.get(ip_address)
        if not matched_device_id:
            new_count += 1

        mac_address = arp_table.get(ip_address)
        vendor_guess = result["vendor_guess"] or (oui_lookup.lookup_oui(mac_address) if mac_address else None)
        ipam_status, ipam_note = _classify_ipam_status(db, ip_address, matched_device_id)

        db.add(
            DiscoveredHost(
                scan_id=scan.id,
                ip_address=ip_address,
                ip_sort_key=_sort_key(ip_address),
                hostname=result["hostname"],
                mac_address=mac_address,
                open_ports=result["open_ports"],
                response_time_ms=result["response_time_ms"],
                snmp_sys_name=result["snmp_sys_name"],
                snmp_sys_descr=result["snmp_sys_descr"],
                vendor_guess=vendor_guess,
                matched_device_id=matched_device_id,
                ipam_status=ipam_status,
                ipam_reservation_note=ipam_note,
            )
        )

    scan.responsive_hosts = responsive
    scan.new_hosts = new_count
    db.add(scan)
    db.commit()
