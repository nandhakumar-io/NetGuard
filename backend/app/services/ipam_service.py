"""IPAM (IP Address Management) core logic.

Deliberately keeps ASSIGNED addresses (real devices) out of the
ip_reservations table -- a device's IP already lives on Device.ip_address,
and duplicating it into a reservation row would just be another place for
the two to drift out of sync. Instead, utilization/listing/conflict logic
below computes "assigned" membership live by checking which devices'
ip_address falls inside a given subnet, and layers the persisted
RESERVED/GATEWAY/BROADCAST/NETWORK reservation rows on top of that.

Only IPv4 is supported (see schemas.subnet.SubnetCreate validator), and
subnets are capped at /16 for the "list every address" / "find free IP"
endpoints (see MAX_ADDRESSES_FOR_ENUMERATION below) -- enumerating a /8
address-by-address in a request/response cycle isn't useful for a fleet
IPAM and would just tie up a worker.

"Used" isn't just Device.ip_address, either. A device's *management* IP
is only one address it holds -- every other interface configured with an
`ip address` line in its running config (sub-interfaces, SVIs, secondary
addresses, point-to-point links, etc.) is just as truly assigned, and
until now IPAM had no idea those existed: an operator could "Find free
IP", get back an address that's actually live on a router sub-interface,
and hand out a duplicate. interface_ips_in_subnet() below closes that gap
by reusing the same config-parsing path app.services.topology_service
already relies on for the same underlying fact (which IPs are actually
configured where) -- so IPAM and Topology can never disagree about what's
in use on the wire.
"""
import ipaddress
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.subnet import IPAddressState, IPReservation, Subnet, SubnetScannedHost
from app.services import risk_engine, snapshot_service

# Above this many total addresses, per-address enumeration (list_addresses /
# find_free_ip) is refused in favor of just the summary utilization -- a
# /16 (65536 addresses) is already a generous ceiling for a single VLAN.
MAX_ADDRESSES_FOR_ENUMERATION = 65536

# Live nmap ping-sweeps scale with host count and network RTT in a way
# enumeration above doesn't (it's real network I/O, not just local
# iteration), so scanning is capped much tighter than listing -- a /22
# (1024 addresses) already takes real wall-clock time even with nmap's
# parallel probing; anything wider should be broken into smaller subnets
# in IPAM rather than tying up a request for minutes.
MAX_ADDRESSES_FOR_SCAN = 1024
_SCAN_TIMEOUT_SECONDS = 120


def network_for(subnet: Subnet) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(subnet.cidr, strict=False)


def _structural_addresses(net: ipaddress.IPv4Network) -> dict[str, str]:
    """Addresses that are unusable purely by virtue of network math --
    network and broadcast address. /31 and /32 have no distinct
    network/broadcast in the usual sense, so those are skipped (every
    address in a /31 is usable per RFC 3021; a /32 is a single host).
    """
    result: dict[str, str] = {}
    if net.prefixlen <= 30:
        result[str(net.network_address)] = IPAddressState.NETWORK.value
        result[str(net.broadcast_address)] = IPAddressState.BROADCAST.value
    return result


def devices_in_subnet(db: Session, net: ipaddress.IPv4Network) -> list[Device]:
    """Devices whose management IP falls inside `net`. Pulled as a plain
    list-and-filter (not a SQL range query) since ip_address is stored as
    a free-text string and Postgres INET range operators aren't in play
    here -- fine at fleet scale (hundreds/low thousands of devices).
    """
    out = []
    for device in db.query(Device).filter(Device.ip_address.isnot(None)).all():
        try:
            if ipaddress.ip_address(device.ip_address) in net:
                out.append(device)
        except ValueError:
            continue  # malformed/legacy ip_address value; ignore rather than 500
    return out


def interface_ips_in_subnet(db: Session, net: ipaddress.IPv4Network) -> dict[str, Device]:
    """Every IP address inside `net` that's actually configured on some
    device's interface -- per the device's latest backed-up running
    config -- keyed by address, mapped back to the device it's on.

    This is deliberately separate from devices_in_subnet(): a device can
    have interface addresses in `net` without its *management* IP being
    in `net` at all (e.g. NetGuard manages it over a dedicated mgmt
    subnet while its data-plane interfaces sit in the VLANs being
    IPAM'd), and a single device can hold several addresses inside the
    same subnet (secondary IPs, HSRP/VRRP virtuals declared as a second
    `ip address ... secondary` line, sub-interfaces). Every managed
    device is checked, not just ones whose mgmt IP already matched, so
    an address doesn't have to also be someone's mgmt IP to count as
    used.

    Best-effort like every other config-derived fact in this codebase:
    a device with no snapshot on file yet (never backed up) simply
    contributes nothing here -- same "we can't invent data we don't
    have" posture as topology_service's subnet-inference tier.
    """
    out: dict[str, Device] = {}
    for device in db.query(Device).all():
        snap = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == device.id)
            .order_by(ConfigSnapshot.seq.desc())
            .first()
        )
        if not snap:
            continue
        try:
            config_text = snapshot_service.decrypt_config(snap.running_config_encrypted)
        except Exception:
            continue
        for ip, mask in risk_engine.parse_config(config_text).ip_addresses:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr not in net:
                continue
            # First device found holding an address wins the label if two
            # devices somehow both claim it -- that's a real conflict, not
            # something to silently resolve here; fleet_conflicts()-style
            # reporting for interface IPs is a reasonable follow-up but
            # out of scope for "is this address free".
            out.setdefault(ip, device)
    return out


def scanned_ips_in_subnet(db: Session, subnet: Subnet) -> dict[str, SubnetScannedHost]:
    """Live hosts from the most recent nmap sweep of this subnet, keyed
    by address -> the SubnetScannedHost row (hostname, and -- once a
    fingerprinting pass has been run -- os_guess/device_type/etc, see
    fingerprint_subnet()). Empty dict if the subnet has never been
    scanned -- callers fall back to the assigned/interface/reserved
    signals only, same as before this feature existed.
    """
    return {
        h.ip_address: h
        for h in db.query(SubnetScannedHost).filter(SubnetScannedHost.subnet_id == subnet.id).all()
    }


def scan_subnet(db: Session, subnet: Subnet) -> dict:
    """Runs a live nmap ping-sweep (`nmap -sn`) over the subnet and
    replaces its stored scan results wholesale, so utilization/listing
    reflects who's *actually on the wire right now* -- not just switches
    and routers NetGuard happens to manage. This is what catches
    unmanaged endpoints (workstations, printers, phones, IoT) that a
    device inventory + config-parsing approach structurally can't see:
    they were never added as a Device and have no running-config for
    interface_ips_in_subnet() to read.

    Requires the `nmap` binary in this runtime's PATH (same
    best-effort-if-present posture as path_trace_service's traceroute/ping
    -- if it's missing, this raises rather than silently returning a
    false "nothing's up", since a scan that didn't actually run must
    never be confused with a scan that ran and found nothing).

    A plain `-sn` ping sweep (ICMP echo + a TCP SYN/ACK probe on common
    ports as a fallback for hosts that block ICMP) needs no elevated
    privileges nmap's SYN/OS-fingerprint scans do, and is exactly what
    "is this address in use" needs -- host discovery, not port/service
    enumeration.
    """
    net = network_for(subnet)
    if net.num_addresses > MAX_ADDRESSES_FOR_SCAN:
        raise ValueError(f"Subnet too large to scan (max {MAX_ADDRESSES_FOR_SCAN} addresses, use a narrower CIDR)")

    nmap_path = shutil.which("nmap")
    if not nmap_path:
        raise RuntimeError(
            "nmap is not installed in this environment. Install the `nmap` package on the NetGuard "
            "backend host to enable live subnet scanning."
        )

    try:
        result = subprocess.run(
            [nmap_path, "-sn", "-n", "-T4", str(net)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_SCAN_TIMEOUT_SECONDS,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"nmap scan of {net} timed out after {_SCAN_TIMEOUT_SECONDS}s -- try a narrower subnet."
        ) from exc

    # Parse nmap's plain-text `-sn` output. Each discovered host is a
    # block starting with either:
    #   "Nmap scan report for 10.0.0.5"                (no rDNS)
    #   "Nmap scan report for host.example.com (10.0.0.5)"  (rDNS resolved)
    hosts: dict[str, str | None] = {}
    for line in (result.stdout or "").splitlines():
        m = re.match(r"^Nmap scan report for (?:(\S+) )?\(?([\d.]+)\)?\s*$", line.strip())
        if not m:
            continue
        name, ip = m.group(1), m.group(2)
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        hosts[ip] = name if name and name != ip else None

    now = datetime.now(timezone.utc)
    db.query(SubnetScannedHost).filter(SubnetScannedHost.subnet_id == subnet.id).delete()
    for ip, hostname in hosts.items():
        db.add(SubnetScannedHost(subnet_id=subnet.id, ip_address=ip, hostname=hostname, scanned_at=now))
    subnet.last_scanned_at = now
    db.commit()

    return {"scanned_at": now.isoformat(), "hosts_found": len(hosts), "addresses_scanned": net.num_addresses}


def due_for_rescan(db: Session) -> list[Subnet]:
    """Subnets whose scheduled auto-rescan cadence has actually elapsed --
    the per-subnet-cadence half of app.tasks.run_subnet_rescan_sweep_task
    (same shape as Device's reachability-poll due check: the beat tick
    fires often, this decides who's actually due). A subnet with
    auto_rescan_enabled but no explicit rescan_interval_hours falls back
    to settings.IPAM_RESCAN_SWEEP_INTERVAL_SECONDS converted to hours, so
    turning the toggle on with no override still does something sane. A
    subnet that's never been scanned is always due.
    """
    from app.core.config import settings

    now = datetime.now(timezone.utc)
    default_hours = max(1, settings.IPAM_RESCAN_SWEEP_INTERVAL_SECONDS // 3600) or 1
    due: list[Subnet] = []
    for subnet in db.query(Subnet).filter(Subnet.auto_rescan_enabled.is_(True)).all():
        if subnet.last_scanned_at is None:
            due.append(subnet)
            continue
        interval_hours = subnet.rescan_interval_hours or default_hours
        elapsed = now - subnet.last_scanned_at
        if elapsed >= timedelta(hours=interval_hours):
            due.append(subnet)
    return due


def fingerprint_subnet(db: Session, subnet: Subnet) -> dict:
    """Runs a live `nmap -O` OS/device-type fingerprint pass over the
    subnet and merges the results onto existing subnet_scanned_hosts
    rows (running scan_subnet() first if the subnet's never been
    ping-swept, since a host has to be found before it can be
    fingerprinted).

    This is a genuinely different deployment story from plain
    scan_subnet()'s `-sn` ping-sweep. `-sn` only ever sends/reads
    ordinary ICMP and TCP packets, which any unprivileged process can
    do. `-O` (and `--osscan-guess`) crafts malformed/unusual TCP, UDP
    and ICMP probes and inspects low-level details of the responses
    (initial window size, TCP option ordering, ISN sampling, IP ID
    behavior, etc.) to fingerprint a stack -- and building/reading raw
    IP packets like that requires a raw socket, which the Linux kernel
    only grants to root or a process with CAP_NET_RAW + CAP_NET_ADMIN.

    Practically, that means:
      - Running the NetGuard backend container as root, OR
      - `docker run --cap-add=NET_RAW --cap-add=NET_ADMIN ...` /
        the Kubernetes-manifest equivalent (securityContext.capabilities),
        so the process stays unprivileged for everything else, OR
      - `setcap cap_net_raw,cap_net_admin+eip $(which nmap)` on the host
        nmap binary, if NetGuard isn't containerized.
    Whichever is chosen, it's an explicit deployment decision -- this
    function is never called implicitly by scan_subnet() or anything
    scheduled, only by an operator opting in from the IPAM UI/API.

    Best-effort per-host: nmap's own OS match confidence
    (os_accuracy, 0-100) is stored as reported rather than filtered by
    NetGuard, and a host nmap couldn't confidently classify simply
    keeps os_guess/device_type as None -- same "don't invent data we
    don't have" posture as the rest of this module.
    """
    net = network_for(subnet)
    if net.num_addresses > MAX_ADDRESSES_FOR_SCAN:
        raise ValueError(f"Subnet too large to fingerprint (max {MAX_ADDRESSES_FOR_SCAN} addresses, use a narrower CIDR)")

    nmap_path = shutil.which("nmap")
    if not nmap_path:
        raise RuntimeError(
            "nmap is not installed in this environment. Install the `nmap` package on the NetGuard "
            "backend host to enable fingerprinting."
        )

    try:
        result = subprocess.run(
            [nmap_path, "-O", "--osscan-guess", "-n", "-T4", "-oX", "-", str(net)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SCAN_TIMEOUT_SECONDS,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"nmap OS fingerprint of {net} timed out after {_SCAN_TIMEOUT_SECONDS}s -- try a narrower subnet."
        ) from exc

    stderr = result.stderr or ""
    if "requires root privileges" in stderr or "requires privileged access" in stderr or "root privileges" in stderr:
        raise PermissionError(
            "OS/device-type fingerprinting needs a raw socket, which this NetGuard backend process doesn't "
            "have. Run the backend as root, or grant it CAP_NET_RAW and CAP_NET_ADMIN (e.g. `docker run "
            "--cap-add=NET_RAW --cap-add=NET_ADMIN`), then retry."
        )

    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(result.stdout or "<nmaprun/>")
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse nmap's fingerprint output: {exc}") from exc

    now = datetime.now(timezone.utc)
    fingerprints: dict[str, dict] = {}
    for host_el in root.findall("host"):
        status = host_el.find("status")
        if status is None or status.get("state") != "up":
            continue
        addr_el = host_el.find("address[@addrtype='ipv4']")
        if addr_el is None:
            continue
        ip = addr_el.get("addr")
        mac_el = host_el.find("address[@addrtype='mac']")
        mac_vendor = mac_el.get("vendor") if mac_el is not None else None

        os_guess = os_accuracy = device_type = None
        osmatch = host_el.find("os/osmatch")
        if osmatch is not None:
            os_guess = osmatch.get("name")
            try:
                os_accuracy = int(osmatch.get("accuracy"))
            except (TypeError, ValueError):
                os_accuracy = None
        osclass = host_el.find("os/osmatch/osclass")
        if osclass is not None and osclass.get("type"):
            device_type = osclass.get("type")  # e.g. "general purpose", "router", "printer", "phone", "WAP"

        fingerprints[ip] = {
            "os_guess": os_guess,
            "os_accuracy": os_accuracy,
            "device_type": device_type,
            "mac_vendor": mac_vendor,
        }

    if not fingerprints:
        # Ensures the subnet has scan rows for -O's own host-discovery to
        # attach onto; a fingerprint pass with nothing to merge into
        # (subnet never pinged-swept, and -O itself found nothing up) is
        # a no-op rather than an error.
        return {"fingerprinted_at": now.isoformat(), "hosts_fingerprinted": 0, "addresses_scanned": net.num_addresses}

    existing = {h.ip_address: h for h in db.query(SubnetScannedHost).filter(SubnetScannedHost.subnet_id == subnet.id).all()}
    updated = 0
    for ip, fp in fingerprints.items():
        row = existing.get(ip)
        if row is None:
            # -O found a live host -sn's ping-sweep hadn't recorded yet
            # (e.g. subnet never scanned before) -- add it so it doesn't
            # get lost.
            row = SubnetScannedHost(subnet_id=subnet.id, ip_address=ip, scanned_at=now)
            db.add(row)
        row.os_guess = fp["os_guess"]
        row.os_accuracy = fp["os_accuracy"]
        row.device_type = fp["device_type"]
        row.mac_vendor = fp["mac_vendor"]
        row.fingerprinted_at = now
        updated += 1

    subnet.last_scanned_at = now
    db.commit()

    return {"fingerprinted_at": now.isoformat(), "hosts_fingerprinted": updated, "addresses_scanned": net.num_addresses}


def subnet_utilization(db: Session, subnet: Subnet) -> dict:
    net = network_for(subnet)
    total = net.num_addresses
    structural = _structural_addresses(net)
    usable = total - len(structural)

    assigned_ips = {d.ip_address for d in devices_in_subnet(db, net)}
    interface_ips = set(interface_ips_in_subnet(db, net).keys())
    reservations = db.query(IPReservation).filter(IPReservation.subnet_id == subnet.id).all()
    reserved_ips = {r.ip_address for r in reservations}
    scanned_ips = set(scanned_ips_in_subnet(db, subnet).keys())

    # Union so an assigned/reserved address that happens to coincide with
    # a structural one (e.g. someone statically set the gateway IP on a
    # device) isn't double counted.
    used_ips = set(structural.keys()) | assigned_ips | interface_ips | reserved_ips | scanned_ips
    used = len(used_ips)
    free = max(total - used, 0)

    return {
        "total_addresses": total,
        "usable_addresses": max(usable, 0),
        "used_count": used,
        "free_count": free,
        "utilization_pct": round((used / total) * 100, 1) if total else 0.0,
        "last_scanned_at": subnet.last_scanned_at.isoformat() if subnet.last_scanned_at else None,
        # How many of the "used" addresses were found *only* by the nmap
        # sweep -- i.e. unmanaged hosts the inventory/config-derived
        # signals alone would have missed and reported as falsely free.
        "scanned_only_count": len(scanned_ips - structural.keys() - assigned_ips - interface_ips - reserved_ips),
    }


def list_addresses(db: Session, subnet: Subnet) -> list[dict]:
    net = network_for(subnet)
    if net.num_addresses > MAX_ADDRESSES_FOR_ENUMERATION:
        raise ValueError(f"Subnet too large to enumerate (max {MAX_ADDRESSES_FOR_ENUMERATION} addresses)")

    structural = _structural_addresses(net)
    devices_by_ip = {d.ip_address: d for d in devices_in_subnet(db, net)}
    interface_devices_by_ip = interface_ips_in_subnet(db, net)
    reservations_by_ip = {r.ip_address: r for r in db.query(IPReservation).filter(IPReservation.subnet_id == subnet.id).all()}
    scanned_by_ip = scanned_ips_in_subnet(db, subnet)

    rows = []
    for addr in net.hosts() if net.prefixlen < 31 else net:
        ip = str(addr)
        if ip in structural:
            rows.append({"ip_address": ip, "state": structural[ip], "device_id": None, "hostname": None, "note": None})
        elif ip in devices_by_ip:
            d = devices_by_ip[ip]
            rows.append({"ip_address": ip, "state": "assigned", "device_id": d.id, "hostname": d.hostname, "note": None})
        elif ip in interface_devices_by_ip:
            d = interface_devices_by_ip[ip]
            # Distinct state from "assigned" so the IPAM UI can tell "this
            # is someone's management IP" apart from "this showed up on an
            # interface in a backed-up config" -- both are equally taken,
            # but the provenance is worth surfacing (e.g. in a tooltip).
            rows.append(
                {"ip_address": ip, "state": "interface", "device_id": d.id, "hostname": d.hostname, "note": None}
            )
        elif ip in reservations_by_ip:
            r = reservations_by_ip[ip]
            rows.append({"ip_address": ip, "state": r.state.value, "device_id": None, "hostname": None, "note": r.note})
        elif ip in scanned_by_ip:
            # Live but otherwise unknown to NetGuard -- an unmanaged host
            # the nmap sweep found and none of the other three signals
            # could have. Surfaces the reverse-DNS name (if any) in `note`
            # since there's no Device row to pull a hostname from, plus
            # OS/device-type fingerprint fields if a fingerprinting pass
            # (not just the plain ping-sweep) has been run.
            h = scanned_by_ip[ip]
            note = "found by nmap scan"
            if h.os_guess:
                note = f"{note} — {h.os_guess} ({h.os_accuracy}% confidence)" if h.os_accuracy is not None else f"{note} — {h.os_guess}"
            rows.append(
                {
                    "ip_address": ip,
                    "state": "scanned",
                    "device_id": None,
                    "hostname": h.hostname,
                    "note": note,
                    "os_guess": h.os_guess,
                    "os_accuracy": h.os_accuracy,
                    "device_type": h.device_type,
                    "mac_vendor": h.mac_vendor,
                    "fingerprinted_at": h.fingerprinted_at,
                }
            )
        else:
            rows.append({"ip_address": ip, "state": "free", "device_id": None, "hostname": None, "note": None})
    # Also surface the network/broadcast addresses themselves for /30 and
    # wider (net.hosts() already excludes them).
    if net.prefixlen <= 30:
        rows.insert(0, {"ip_address": str(net.network_address), "state": "network", "device_id": None, "hostname": None, "note": None})
        rows.append({"ip_address": str(net.broadcast_address), "state": "broadcast", "device_id": None, "hostname": None, "note": None})
    return rows


def find_free_ip(db: Session, subnet: Subnet) -> str | None:
    net = network_for(subnet)
    if net.num_addresses > MAX_ADDRESSES_FOR_ENUMERATION:
        raise ValueError(f"Subnet too large to enumerate (max {MAX_ADDRESSES_FOR_ENUMERATION} addresses)")

    taken = set(_structural_addresses(net).keys())
    taken |= {d.ip_address for d in devices_in_subnet(db, net)}
    taken |= set(interface_ips_in_subnet(db, net).keys())
    taken |= {r.ip_address for r in db.query(IPReservation).filter(IPReservation.subnet_id == subnet.id).all()}
    taken |= set(scanned_ips_in_subnet(db, subnet).keys())

    candidates = net.hosts() if net.prefixlen < 31 else net
    for addr in candidates:
        ip = str(addr)
        if ip not in taken:
            return ip
    return None


def find_subnet_for_ip(db: Session, ip_address: str) -> Subnet | None:
    """Which managed subnet (if any) an address falls inside -- used by
    device create/update to flag static-assignment conflicts even when
    the caller didn't specify a subnet explicitly. If an IP falls inside
    more than one configured subnet (overlapping ranges -- a
    misconfiguration, but not this function's job to police), the
    smallest (most specific) one wins.
    """
    try:
        addr = ipaddress.ip_address(ip_address)
    except ValueError:
        return None

    matches = []
    for subnet in db.query(Subnet).all():
        try:
            net = network_for(subnet)
        except ValueError:
            continue
        if addr in net:
            matches.append((net.prefixlen, subnet))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


def check_conflict(db: Session, ip_address: str, exclude_device_id=None) -> list[Device]:
    """Other active devices already sitting on `ip_address` -- either as
    their management IP, or as an address configured on one of their
    interfaces per their latest backed-up config. Used both as a
    standalone "is this IP already in use" check (device create/update)
    and by the fleet-wide conflicts report below.
    """
    q = db.query(Device).filter(Device.ip_address == ip_address)
    if exclude_device_id is not None:
        q = q.filter(Device.id != exclude_device_id)
    conflicting = {d.id: d for d in q.all()}

    try:
        addr = ipaddress.ip_address(ip_address)
    except ValueError:
        return list(conflicting.values())
    single_host_net = ipaddress.ip_network(f"{addr}/32")
    for ip, device in interface_ips_in_subnet(db, single_host_net).items():
        if exclude_device_id is not None and device.id == exclude_device_id:
            continue
        conflicting.setdefault(device.id, device)

    return list(conflicting.values())


def fleet_conflicts(db: Session) -> list[dict]:
    """Every IP address currently claimed by more than one device --
    i.e. an actual conflict already present in inventory, independent of
    any subnet being defined for it. This is the "did someone statically
    assign an IP that's already in use" check the NOC actually wants,
    not gated on IPAM adoption.
    """
    by_ip: dict[str, list[Device]] = {}
    for device in db.query(Device).filter(Device.ip_address.isnot(None)).all():
        by_ip.setdefault(device.ip_address, []).append(device)

    return [
        {"ip_address": ip, "device_ids": [d.id for d in devices], "hostnames": [d.hostname for d in devices]}
        for ip, devices in sorted(by_ip.items())
        if len(devices) > 1
    ]


def stale_reservations(db: Session, subnet: Subnet | None = None) -> list[dict]:
    """RESERVED IPReservations that discovery scans haven't found a live
    host at -- the natural complement to the discovery-side rogue/expected
    split (app.services.network_discovery_service._classify_ipam_status):
    that side flags "something's here IPAM didn't plan for", this flags
    "IPAM planned for something here and it never showed up" (a rollout
    that stalled, or a reservation nobody cleaned up after the project
    that needed it wrapped or moved).

    Only ever compares against completed DiscoveryScan/DiscoveredHost
    data already on file -- never triggers a scan itself, since this is a
    read/report endpoint, not an action. A reservation whose address was
    never covered by any scan's CIDR is reported as coverage="never_scanned"
    rather than lumped in with "scanned and nothing answered": those are
    very different situations for an admin (go run a scan vs. this is
    genuinely looking stale) and conflating them would make the stale
    list untrustworthy.
    """
    query = db.query(IPReservation).filter(IPReservation.state == IPAddressState.RESERVED)
    if subnet is not None:
        query = query.filter(IPReservation.subnet_id == subnet.id)
    reservations = query.order_by(IPReservation.ip_address.asc()).all()
    if not reservations:
        return []

    from app.models.network_discovery import (
        DiscoveredHost,
        DiscoveryScan,
        DiscoveryScanStatus,
    )

    subnets_by_id = {subnet.id: subnet} if subnet is not None else {s.id: s for s in db.query(Subnet).all()}
    completed_scans = (
        db.query(DiscoveryScan)
        .filter(DiscoveryScan.status == DiscoveryScanStatus.COMPLETED)
        .order_by(DiscoveryScan.completed_at.desc())
        .all()
    )
    # Pre-parse each scan's CIDR once rather than per-reservation.
    scan_networks = []
    for scan in completed_scans:
        try:
            scan_networks.append((scan, ipaddress.ip_network(scan.cidr, strict=False)))
        except ValueError:
            continue

    results: list[dict] = []
    for reservation in reservations:
        sub = subnets_by_id.get(reservation.subnet_id)
        if sub is None:
            continue
        try:
            addr = ipaddress.ip_address(reservation.ip_address)
        except ValueError:
            continue

        # Most recently completed scan whose range covers this address --
        # "most recent" so a reservation that was genuinely fulfilled
        # after an older, narrower scan missed it isn't reported stale
        # forever just because some other scan once covered the range.
        covering_scan = next((scan for scan, net in scan_networks if addr in net), None)
        if covering_scan is None:
            results.append(
                {
                    "reservation_id": reservation.id,
                    "subnet_id": sub.id,
                    "subnet_cidr": sub.cidr,
                    "ip_address": reservation.ip_address,
                    "note": reservation.note,
                    "reserved_at": reservation.created_at,
                    "coverage": "never_scanned",
                    "last_scan_at": None,
                }
            )
            continue

        seen = (
            db.query(DiscoveredHost)
            .filter(DiscoveredHost.scan_id == covering_scan.id, DiscoveredHost.ip_address == reservation.ip_address)
            .first()
        )
        if seen is None:
            results.append(
                {
                    "reservation_id": reservation.id,
                    "subnet_id": sub.id,
                    "subnet_cidr": sub.cidr,
                    "ip_address": reservation.ip_address,
                    "note": reservation.note,
                    "reserved_at": reservation.created_at,
                    "coverage": "scanned_no_response",
                    "last_scan_at": covering_scan.completed_at,
                }
            )

    return results
