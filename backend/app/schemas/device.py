import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.device import DeviceVendor, DeviceStatus, SnmpVersion, SnmpSecurityLevel, SnmpAuthProtocol, SnmpPrivProtocol


class DeviceBase(BaseModel):
    hostname: str
    ip_address: str
    vendor: DeviceVendor = DeviceVendor.CISCO
    site: str | None = None
    device_type: str | None = None
    ssh_username: str | None = None
    ssh_credential_ref: str | None = None

    # --- SNMP Monitoring (Health Dashboard) ---
    supports_snmp: bool = False
    snmp_version: SnmpVersion | None = None
    snmp_port: int | None = 161
    snmp_community_ref: str | None = None  # v1/v2c (legacy env-var fallback)
    snmp_username: str | None = None  # v3
    snmp_auth_credential_ref: str | None = None  # v3 auth passphrase (legacy env-var fallback)
    snmp_privacy_credential_ref: str | None = None  # v3 priv passphrase (legacy env-var fallback)
    # v3 USM parameters -- not secrets, just protocol choice (the actual
    # auth/priv passphrases are set via POST /devices/{id}/snmp-credentials
    # and are never exposed through this schema; see DeviceRead below).
    snmp_security_level: SnmpSecurityLevel | None = None
    snmp_auth_protocol: SnmpAuthProtocol | None = None
    snmp_priv_protocol: SnmpPrivProtocol | None = None

    # --- NETCONF / RESTCONF (Protocol Manager) ---
    supports_netconf: bool = False
    netconf_port: int | None = 830
    # Whether push_config/restore_config should <lock>/<unlock> the
    # datastore around edit-config. Off for devices whose NETCONF agent
    # doesn't implement (or rejects) <lock> -- otherwise every deploy to
    # that device fails at the lock step. See Device.netconf_use_lock.
    netconf_use_lock: bool = True
    supports_restconf: bool = False
    restconf_url: str | None = None

    # --- Discovered / manually-entered inventory detail ---
    platform: str | None = None
    model: str | None = None
    serial_number: str | None = None
    os_version: str | None = None
    capabilities: str | None = None

    # --- Lab / simulation backing (GNS3) ---
    is_simulated: bool = False
    lab_provider: str | None = None
    gns3_project_id: str | None = None
    gns3_node_id: str | None = None
    console_host: str | None = None
    console_port: int | None = None
    console_type: str | None = None
    bootstrapped: bool = False


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    hostname: str | None = None
    ip_address: str | None = None
    vendor: DeviceVendor | None = None
    site: str | None = None
    device_type: str | None = None
    ssh_username: str | None = None
    ssh_credential_ref: str | None = None
    supports_snmp: bool | None = None
    snmp_version: SnmpVersion | None = None
    snmp_port: int | None = None
    snmp_community_ref: str | None = None
    snmp_username: str | None = None
    snmp_auth_credential_ref: str | None = None
    snmp_privacy_credential_ref: str | None = None
    snmp_security_level: SnmpSecurityLevel | None = None
    snmp_auth_protocol: SnmpAuthProtocol | None = None
    snmp_priv_protocol: SnmpPrivProtocol | None = None
    supports_netconf: bool | None = None
    netconf_port: int | None = None
    netconf_use_lock: bool | None = None
    supports_restconf: bool | None = None
    restconf_url: str | None = None
    platform: str | None = None
    model: str | None = None
    serial_number: str | None = None
    os_version: str | None = None
    capabilities: str | None = None
    is_simulated: bool | None = None
    lab_provider: str | None = None
    gns3_project_id: str | None = None
    gns3_node_id: str | None = None
    console_host: str | None = None
    console_port: int | None = None
    console_type: str | None = None
    bootstrapped: bool | None = None


class DeviceRead(DeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DeviceStatus
    flagged_unstable: bool = False
    unstable_since: datetime.datetime | None = None
    # Derived, not stored: whether *some* SNMP secret is on file for this
    # device (community for v1/v2c, or a v3 auth/priv key), without ever
    # exposing the encrypted columns themselves through this schema. Lets
    # the UI show "credentials configured" vs. "not configured" honestly.
    snmp_credentials_configured: bool = False
    # Same idea for the DB-encrypted SSH password (POST
    # /devices/{id}/ssh-credentials). ssh_credential_ref itself is already
    # plain on DeviceBase (it's just a pointer, not a secret) -- this flag
    # is specifically about whether a real password is on file.
    ssh_credentials_configured: bool = False

    @classmethod
    def from_device(cls, device) -> "DeviceRead":
        obj = cls.model_validate(device)
        obj.snmp_credentials_configured = bool(
            device.snmp_community_encrypted or device.snmp_auth_key_encrypted or device.snmp_priv_key_encrypted
        )
        obj.ssh_credentials_configured = bool(device.ssh_password_encrypted)
        return obj


class SnmpCredentialsUpdate(BaseModel):
    """Body for POST /devices/{id}/snmp-credentials. Only fields actually
    provided are touched/encrypted -- omit a field (or send it as null)
    to leave the currently-stored value untouched; send "" to explicitly
    clear it. Never round-tripped back out via DeviceRead.
    """

    community: str | None = None  # v1/v2c
    v3_auth_key: str | None = None  # v3 auth passphrase (authNoPriv/authPriv)
    v3_priv_key: str | None = None  # v3 privacy passphrase (authPriv)


class SnmpTestResult(BaseModel):
    success: bool
    message: str
    sys_descr: str | None = None
    sys_uptime_seconds: int | None = None


class SshCredentialsUpdate(BaseModel):
    """Body for POST /devices/{id}/ssh-credentials. Mirrors
    SnmpCredentialsUpdate: only fields actually provided are touched.
    username is stored plain (it's not a secret, same as ssh_username on
    DeviceBase always was); password is encrypted at rest and never
    returned by any GET endpoint. Send password="" to explicitly clear it.
    """

    username: str | None = None
    password: str | None = None


class SshTestResult(BaseModel):
    success: bool
    message: str
    protocol: str | None = None  # which transport actually answered: netconf / restconf / ssh / telnet


class ArpEntry(BaseModel):
    if_index: str
    ip_address: str
    mac_address: str


class RouteEntry(BaseModel):
    destination: str
    mask: str | None = None
    next_hop: str
    if_index: str | None = None


class LldpNeighbor(BaseModel):
    local_port_index: str
    neighbor_name: str | None = None
    neighbor_port: str | None = None


class CdpNeighbor(BaseModel):
    local_if_index: str
    neighbor_id: str | None = None
    neighbor_port: str | None = None
    neighbor_platform: str | None = None


class InventoryItem(BaseModel):
    index: str
    name: str | None = None
    description: str | None = None
    model: str | None = None
    serial_number: str | None = None


class DeviceDiscoveryResult(BaseModel):
    """SNMP-based discovery for Cisco devices: hostname, ARP table,
    routing table, LLDP/CDP neighbors, and chassis/module inventory.
    Every sub-list is independently best-effort -- an empty list means
    that table wasn't available/populated on the device, not that the
    whole discovery call failed (see snmp_service.discover_inventory)."""

    device_id: uuid.UUID
    hostname: str | None = None
    reported_hostname: str | None = None  # device's own sysName, if it answered
    arp_table: list[ArpEntry] = Field(default_factory=list)
    routing_table: list[RouteEntry] = Field(default_factory=list)
    lldp_neighbors: list[LldpNeighbor] = Field(default_factory=list)
    cdp_neighbors: list[CdpNeighbor] = Field(default_factory=list)
    inventory: list[InventoryItem] = Field(default_factory=list)
    retrieved_at: datetime.datetime