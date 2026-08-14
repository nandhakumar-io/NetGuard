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

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.subnet import IPAddressState, IPReservation, Subnet
from app.services import risk_engine, snapshot_service

# Above this many total addresses, per-address enumeration (list_addresses /
# find_free_ip) is refused in favor of just the summary utilization -- a
# /16 (65536 addresses) is already a generous ceiling for a single VLAN.
MAX_ADDRESSES_FOR_ENUMERATION = 65536


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


def subnet_utilization(db: Session, subnet: Subnet) -> dict:
    net = network_for(subnet)
    total = net.num_addresses
    structural = _structural_addresses(net)
    usable = total - len(structural)

    assigned_ips = {d.ip_address for d in devices_in_subnet(db, net)}
    interface_ips = set(interface_ips_in_subnet(db, net).keys())
    reservations = db.query(IPReservation).filter(IPReservation.subnet_id == subnet.id).all()
    reserved_ips = {r.ip_address for r in reservations}

    # Union so an assigned/reserved address that happens to coincide with
    # a structural one (e.g. someone statically set the gateway IP on a
    # device) isn't double counted.
    used_ips = set(structural.keys()) | assigned_ips | interface_ips | reserved_ips
    used = len(used_ips)
    free = max(total - used, 0)

    return {
        "total_addresses": total,
        "usable_addresses": max(usable, 0),
        "used_count": used,
        "free_count": free,
        "utilization_pct": round((used / total) * 100, 1) if total else 0.0,
    }


def list_addresses(db: Session, subnet: Subnet) -> list[dict]:
    net = network_for(subnet)
    if net.num_addresses > MAX_ADDRESSES_FOR_ENUMERATION:
        raise ValueError(f"Subnet too large to enumerate (max {MAX_ADDRESSES_FOR_ENUMERATION} addresses)")

    structural = _structural_addresses(net)
    devices_by_ip = {d.ip_address: d for d in devices_in_subnet(db, net)}
    interface_devices_by_ip = interface_ips_in_subnet(db, net)
    reservations_by_ip = {r.ip_address: r for r in db.query(IPReservation).filter(IPReservation.subnet_id == subnet.id).all()}

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
