import enum
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class IPAddressState(str, enum.Enum):
    """Why a given address inside a subnet is considered "used" up. Kept
    separate from "is a Device sitting on it" -- GATEWAY/BROADCAST/NETWORK
    are structurally unusable regardless of any device, RESERVED is an
    admin-set hold (e.g. a future VIP, a DHCP scope boundary) with no
    device attached, and ASSIGNED is the normal case of a device's
    ip_address landing inside the subnet.
    """

    ASSIGNED = "assigned"  # matches a Device.ip_address
    RESERVED = "reserved"  # admin hold, no device
    GATEWAY = "gateway"
    BROADCAST = "broadcast"
    NETWORK = "network"


class Subnet(Base):
    """A managed IPv4 block (e.g. 10.20.30.0/24) plus its VLAN tag and
    site, backing the IPAM inventory: utilization %, free-IP lookup, and
    static-assignment conflict detection against Device.ip_address. This
    is deliberately its own table rather than fields bolted onto Device --
    a subnet exists (and can be planned/reserved-in) independent of
    whether any device has been racked into it yet.
    """

    __tablename__ = "subnets"
    __table_args__ = (UniqueConstraint("cidr", name="uq_subnets_cidr"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Canonical form, e.g. "10.20.30.0/24" -- always the network address
    # (not an arbitrary host address) with prefix length. Validated/
    # normalized in schemas.subnet / services.ipam_service before write.
    cidr = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    vlan_id = Column(Integer, nullable=True, index=True)
    site = Column(String, nullable=True, index=True)
    description = Column(Text, nullable=True)
    # Free-text, same convention as Device.tags -- JSON-encoded list.
    tags = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    reservations = relationship(
        "IPReservation", back_populates="subnet", cascade="all, delete-orphan", passive_deletes=True
    )
    scanned_hosts = relationship(
        "SubnetScannedHost", back_populates="subnet", cascade="all, delete-orphan", passive_deletes=True
    )
    # Set on every completed nmap scan (see app.services.ipam_service.scan_subnet)
    # -- lets the IPAM UI show "scanned 4 minutes ago" / "never scanned"
    # next to the utilization figure, so an operator knows whether "used"
    # reflects live reality or is still purely inventory+config-derived.
    last_scanned_at = Column(DateTime(timezone=True), nullable=True)


class IPReservation(Base):
    """A single address inside a Subnet that's held out of the "free"
    pool for a reason other than an active Device sitting on it --
    RESERVED (admin hold), GATEWAY, BROADCAST, or NETWORK (see
    IPAddressState). ASSIGNED addresses are *not* stored here: those are
    derived live from Device.ip_address at read time (see
    services.ipam_service.utilization), so a device's IP never needs to
    be kept in sync in two places. A row here with state=ASSIGNED would
    be ambiguous, so writes are restricted to the other three states.
    """

    __tablename__ = "ip_reservations"
    __table_args__ = (UniqueConstraint("subnet_id", "ip_address", name="uq_ip_reservations_subnet_ip"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subnet_id = Column(UUID(as_uuid=True), ForeignKey("subnets.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_address = Column(String, nullable=False, index=True)
    state = Column(Enum(IPAddressState), nullable=False, default=IPAddressState.RESERVED)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    subnet = relationship("Subnet", back_populates="reservations")


class SubnetScannedHost(Base):
    """One live host found by the most recent nmap ping-sweep of a
    Subnet (see app.services.ipam_service.scan_subnet). Replaced wholesale
    on every scan (old rows for the subnet deleted, new ones inserted) --
    this is a point-in-time "what answered just now" snapshot, not a
    history log, since a host that stops responding should stop counting
    as used the next time someone scans, not linger forever.

    This is what closes the gap the ASSIGNED/interface-IP/RESERVED
    tiers above can't: an unmanaged host (PC, printer, IoT device, phone
    on Wi-Fi) that holds an address in the subnet but was never entered
    into NetGuard's device inventory and has no config for
    interface_ips_in_subnet() to parse. A ping sweep is the only one of
    the four "used" signals that sees it.
    """

    __tablename__ = "subnet_scanned_hosts"
    __table_args__ = (UniqueConstraint("subnet_id", "ip_address", name="uq_subnet_scanned_hosts_subnet_ip"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subnet_id = Column(UUID(as_uuid=True), ForeignKey("subnets.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_address = Column(String, nullable=False, index=True)
    # Reverse-DNS hostname if the scan could resolve one, else None --
    # best-effort identification for an address IPAM otherwise has no
    # name for (not a managed Device, so no Device.hostname to show).
    hostname = Column(String, nullable=True)
    # OS/device-type fingerprint from an opt-in `nmap -O` pass (see
    # app.services.ipam_service.fingerprint_subnet). Unlike the plain
    # -sn ping-sweep above, -O needs to craft and read raw IP packets to
    # tell one TCP/IP stack's fingerprint from another's, which the
    # kernel only allows a raw socket to do -- so these stay NULL until
    # someone explicitly runs a fingerprinting pass on a NetGuard
    # backend that has that capability (root, or CAP_NET_RAW+CAP_NET_ADMIN).
    os_guess = Column(String, nullable=True)
    os_accuracy = Column(Integer, nullable=True)  # nmap's own 0-100 confidence for os_guess
    device_type = Column(String, nullable=True)  # nmap osclass "type", e.g. "router", "printer", "phone"
    mac_vendor = Column(String, nullable=True)  # from ARP-derived MAC OUI, only ever populated when on-link
    fingerprinted_at = Column(DateTime(timezone=True), nullable=True)
    scanned_at = Column(DateTime(timezone=True), server_default=func.now())

    subnet = relationship("Subnet", back_populates="scanned_hosts")
