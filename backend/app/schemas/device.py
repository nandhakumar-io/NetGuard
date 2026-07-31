import uuid

from pydantic import BaseModel, ConfigDict

from app.models.device import DeviceVendor, DeviceStatus


class DeviceBase(BaseModel):
    hostname: str
    ip_address: str
    vendor: DeviceVendor = DeviceVendor.CISCO
    site: str | None = None
    device_type: str | None = None
    ssh_username: str | None = None
    ssh_credential_ref: str | None = None


class DeviceCreate(DeviceBase):
    pass


class DeviceRead(DeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DeviceStatus