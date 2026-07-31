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


class DeviceRead(DeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DeviceStatus