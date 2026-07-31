import enum
import uuid

from sqlalchemy import Boolean, Column, String, Enum, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceVendor(str, enum.Enum):
    CISCO = "cisco"
    JUNIPER = "juniper"
    ARISTA = "arista"
    LINUX = "linux"


class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class SnmpVersion(str, enum.Enum):
    V1 = "v1"
    V2C = "v2c"
    V3 = "v3"


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname = Column(String, unique=True, index=True, nullable=False)
    ip_address = Column(String, nullable=False)  # management IP
    vendor = Column(Enum(DeviceVendor), nullable=False, default=DeviceVendor.CISCO)
    site = Column(String, nullable=True)
    device_type = Column(String, nullable=True)  # e.g. router, switch, firewall
    status = Column(Enum(DeviceStatus), nullable=False, default=DeviceStatus.UNKNOWN)
    ssh_username = Column(String, nullable=True)
    ssh_credential_ref = Column(String, nullable=True)  # pointer to secret store, not raw secret
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- Inventory detail (Device Inventory page columns) ---
    platform = Column(String, nullable=True)  # e.g. "IOS-XE", "Junos", "EOS"
    model = Column(String, nullable=True)  # e.g. "ISR4331"
    serial_number = Column(String, nullable=True)
    os_version = Column(String, nullable=True)

    # --- Protocol support flags (NETCONF / RESTCONF / SNMP) ---
    supports_netconf = Column(Boolean, nullable=False, default=False, server_default="false")
    supports_restconf = Column(Boolean, nullable=False, default=False, server_default="false")
    supports_snmp = Column(Boolean, nullable=False, default=False, server_default="false")

    # --- NETCONF connection settings (ncclient) ---
    netconf_port = Column(Integer, nullable=True, default=830)

    # --- RESTCONF connection settings ---
    restconf_url = Column(String, nullable=True)  # e.g. https://10.0.0.1/restconf

    # --- SNMP connection settings ---
    snmp_version = Column(Enum(SnmpVersion), nullable=True)
    snmp_community_ref = Column(String, nullable=True)  # pointer to secret store (v1/v2c)
    snmp_username = Column(String, nullable=True)  # v3
    snmp_auth_credential_ref = Column(String, nullable=True)  # v3 auth passphrase (secret store ref)
    snmp_privacy_credential_ref = Column(String, nullable=True)  # v3 priv passphrase (secret store ref)

    # --- Discovered capabilities, e.g. NETCONF <hello> capability list, stored as JSON text ---
    capabilities = Column(Text, nullable=True)

    # --- Lab / simulation backing (GNS3 integration) ---
    # A device backed by a GNS3 node is a real virtual router/switch instance
    # (IOSv, vIOS-L2, Arista vEOS, Juniper vMX, ...) running inside GNS3, not a
    # mock. Once `bootstrapped` is true it has a real management IP + SSH
    # reachable from this app, so every other service (deployment_engine,
    # protocol_manager, health_monitor, rollback_service) treats it exactly
    # like a physical device via the normal ip_address/ssh_username columns
    # above -- no separate code path needed for day-to-day deploy/validate/
    # rollback. These columns exist only for: (a) telling lab devices apart
    # in inventory/UI, (b) remembering which GNS3 project/node backs a
    # device so it can be started/stopped/torn down, and (c) reaching the
    # node's console over telnet for the one-time bootstrap before it has an
    # SSH-reachable management IP of its own.
    is_simulated = Column(Boolean, nullable=False, default=False, server_default="false")
    lab_provider = Column(String, nullable=True)  # e.g. "gns3"
    gns3_project_id = Column(String, nullable=True)
    gns3_node_id = Column(String, nullable=True)
    console_host = Column(String, nullable=True)  # GNS3 server address for console access
    console_port = Column(Integer, nullable=True)  # per-node telnet console port assigned by GNS3
    console_type = Column(String, nullable=True, default="telnet")  # telnet | vnc | none
    bootstrapped = Column(Boolean, nullable=False, default=False, server_default="false")