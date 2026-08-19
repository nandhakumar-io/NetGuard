"""Network Discovery: scan a CIDR range for live hosts not yet in the
device inventory, so an operator can pull them into NetGuard without
typing every IP by hand.

Two tables: DiscoveryScan is one "scan a range" job (what was asked for,
its status, and summary counts); DiscoveredHost is one live IP found by
that job, enriched with best-effort identification (reverse DNS, SNMP
sysName/sysDescr when a community string was supplied) and a link to a
matching existing Device row when one already exists, so the results
screen can distinguish "new" from "already known" instead of just
dumping raw IPs.

Deliberately its own pair of tables rather than writing straight into
Device -- a discovered host is a *candidate*, not inventory, until an
operator reviews and imports it (see app.api.network_discovery.import_host).
Mirrors the same "never silently mutate inventory from a background scan"
posture as DiscoveredNeighbor (per-device LLDP/CDP) and DriftBaseline
(per-device compliance) -- this is the subnet-wide sibling of those,
finding devices NetGuard doesn't know about yet rather than checking
ones it does.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class DiscoveryScanStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DiscoveryScan(Base):
    __tablename__ = "discovery_scans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # e.g. "10.0.4.0/24" -- validated and size-capped in
    # app.services.network_discovery_service.parse_and_validate_cidr
    # before a scan is ever enqueued.
    cidr = Column(String, nullable=False)

    # Optional SNMP v2c community string used to probe responsive hosts
    # for sysName/sysDescr identification. Not persisted in the clear --
    # see snmp_community_ref below, same write-once/reference pattern as
    # Device.snmp_community_ref (app.services.credential_service).
    snmp_community_ref = Column(String, nullable=True)
    ports = Column(String, nullable=True)  # comma-separated TCP ports probed, e.g. "22,80,443,161"

    status = Column(Enum(DiscoveryScanStatus), nullable=False, default=DiscoveryScanStatus.PENDING)
    error = Column(Text, nullable=True)

    total_hosts = Column(Integer, nullable=False, default=0)  # size of the range scanned
    responsive_hosts = Column(Integer, nullable=False, default=0)  # hosts that answered on any probed port
    new_hosts = Column(Integer, nullable=False, default=0)  # responsive hosts with no matching Device row

    started_by = Column(String, nullable=True)  # user email
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Set when this scan was fired by a DiscoverySchedule sweep rather
    # than a manual POST /discovery/scans -- lets the schedule sweep task
    # tell "brand new to inventory" apart from "new since the schedule's
    # own last run" (see app.tasks.run_discovery_schedule_sweep_task)
    # without needing a separate results table just for schedules.
    schedule_id = Column(UUID(as_uuid=True), ForeignKey("discovery_schedules.id"), nullable=True, index=True)

    hosts = relationship(
        "DiscoveredHost", back_populates="scan",
        cascade="all, delete-orphan", order_by="DiscoveredHost.ip_sort_key",
    )
    schedule = relationship("DiscoverySchedule", back_populates="scans", foreign_keys=[schedule_id])


class DiscoverySchedule(Base):
    """A saved "re-sweep this range every N minutes" definition.
    app.tasks.run_discovery_schedule_sweep_task (Celery beat, see
    celery_app.py's beat_schedule) fires enabled schedules that are due
    and, if the resulting scan turns up any genuinely new host (not
    already in inventory, not seen on an earlier run of this same
    schedule), fans out through app.services.notification_service.notify
    -- the same webhook/email/in-app channel every other NetGuard alert
    uses, so "a new device showed up on this VLAN" reaches Slack/webhooks
    the same way "a deployment failed" does, no separate integration
    needed.
    """

    __tablename__ = "discovery_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name = Column(String, nullable=False)
    cidr = Column(String, nullable=False)
    snmp_community_ref = Column(String, nullable=True)
    ports = Column(String, nullable=True)

    interval_minutes = Column(Integer, nullable=False, default=1440)  # default: daily
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_scan_id = Column(UUID(as_uuid=True), ForeignKey("discovery_scans.id"), nullable=True)

    scans = relationship("DiscoveryScan", back_populates="schedule", foreign_keys="[DiscoveryScan.schedule_id]")


class DiscoveredHost(Base):
    """One live IP found during a DiscoveryScan. `imported` flips to True
    once an operator turns this into a real Device (app.api
    .network_discovery.import_host) -- kept as a row rather than deleted
    on import so the scan's results view still shows what was found and
    what's already been actioned.
    """

    __tablename__ = "discovered_hosts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_id = Column(UUID(as_uuid=True), ForeignKey("discovery_scans.id"), nullable=False, index=True)

    ip_address = Column(String, nullable=False)
    # Zero-padded sortable form of ip_address (e.g. "010.000.004.017") so
    # the results table can ORDER BY it numerically instead of the
    # lexicographic sort a plain string ORDER BY ip_address would give
    # ("10.0.4.2" sorting after "10.0.4.17").
    ip_sort_key = Column(String, nullable=False, index=True)

    hostname = Column(String, nullable=True)  # reverse DNS, best-effort
    mac_address = Column(String, nullable=True)  # from the local ARP/neighbor table when resolvable
    open_ports = Column(String, nullable=True)  # comma-separated TCP ports that answered

    snmp_sys_name = Column(String, nullable=True)
    snmp_sys_descr = Column(Text, nullable=True)
    vendor_guess = Column(String, nullable=True)  # heuristic from sysDescr / OUI, see network_discovery_service

    response_time_ms = Column(Float, nullable=True)

    # Populated when ip_address matches an existing Device.ip_address --
    # lets the results screen say "already in inventory as <hostname>"
    # instead of offering to re-import a device NetGuard already tracks.
    matched_device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True)

    imported = Column(Boolean, nullable=False, default=False, server_default="false")
    imported_device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True)
    ignored = Column(Boolean, nullable=False, default=False, server_default="false")

    discovered_at = Column(DateTime(timezone=True), server_default=func.now())

    scan = relationship("DiscoveryScan", back_populates="hosts")
