import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.device import DeviceVendor, DeviceStatus, SnmpVersion


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
    snmp_community_ref: str | None = None  # v1/v2c
    snmp_username: str | None = None  # v3
    snmp_auth_credential_ref: str | None = None  # v3 auth passphrase
    snmp_privacy_credential_ref: str | None = None  # v3 priv passphrase

    # --- NETCONF / RESTCONF (Protocol Manager) ---
    supports_netconf: bool = False
    netconf_port: int | None = 830
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
    snmp_community_ref: str | None = None
    snmp_username: str | None = None
    snmp_auth_credential_ref: str | None = None
    snmp_privacy_credential_ref: str | None = None
    supports_netconf: bool | None = None
    netconf_port: int | None = None
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