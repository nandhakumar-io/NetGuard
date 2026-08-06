import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class MaintenanceWindowBase(BaseModel):
    name: str
    reason: str | None = None
    scope: str = "device"
    device_id: uuid.UUID | None = None
    site: str | None = None
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _check_scope_target(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.scope == "device" and not self.device_id:
            raise ValueError("device_id is required for a device-scoped window")
        if self.scope == "site" and not self.site:
            raise ValueError("site is required for a site-scoped window")
        return self


class MaintenanceWindowCreate(MaintenanceWindowBase):
    pass


class MaintenanceWindowRead(MaintenanceWindowBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cancelled: bool
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    created_by: str
    created_at: datetime
    is_active: bool = False
