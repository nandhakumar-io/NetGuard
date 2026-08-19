import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DiscoveredNeighbor(Base):
    """A single LLDP or CDP neighbor row captured by an SNMP discovery run
    (see app.services.snmp_service.discover_inventory, wired up in
    app.api.devices.discover_device).

    One row per (device_id, local_port, neighbor_id) as of the most recent
    discovery -- a fresh discovery run replaces all prior rows for that
    device (see devices.py) rather than accumulating history, since this
    powers a live "who is this device plugged into right now" view, not an
    audit trail.

    `neighbor_device_id` is resolved best-effort at write time by matching
    the raw neighbor hostname/IP reported by the protocol against the
    known device inventory (see app.services.topology_service) -- it's
    nullable because a neighbor may be a device NetGuard doesn't manage
    (e.g. an unmanaged switch or an end host), in which case we still keep
    the raw identity fields for display even though there's no local
    device row to link to.
    """

    __tablename__ = "discovered_neighbors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    protocol = Column(String(8), nullable=False)  # 'lldp' | 'cdp'
    local_port = Column(String(64), nullable=True)
    neighbor_name = Column(String(255), nullable=True)  # lldpRemSysName / cdpCacheDeviceId
    neighbor_port = Column(String(255), nullable=True)  # lldpRemPortId / cdpCacheDevicePort
    neighbor_platform = Column(String(255), nullable=True)  # cdpCachePlatform (LLDP has no direct analog used here)

    # Best-effort link back to a managed device, resolved by hostname/IP
    # match against the neighbor_name at write time. Null = unmanaged/
    # unrecognized neighbor.
    neighbor_device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    # Best-effort switchport enrichment for the *local* port -- same
    # Junos-config/Q-BRIDGE-MIB lookup that backs the device Interfaces
    # tab (see snmp_service.walk_switchport_vlans /
    # config_format_service.parse_junos_switchport_config), applied to
    # LLDP/CDP neighbor rows in app.api.devices._persist_discovered_neighbors
    # so the Discovery and Topology pages can show trunk/access mode and
    # VLAN alongside each confirmed link, not just that it exists.
    port_mode = Column(String(16), nullable=True)  # "access" | "trunk" | "routed" | None
    vlan = Column(String(32), nullable=True)  # access/native VLAN ID
    trunk_vlans = Column(Text, nullable=True)  # comma-separated tagged VLAN IDs, only set when port_mode == "trunk"

    discovered_at = Column(DateTime(timezone=True), server_default=func.now())
