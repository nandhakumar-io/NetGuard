import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class DeviceGroupCreate(BaseModel):
    name: str
    description: str | None = None
    group_type: str = "static"
    parent_group_id: uuid.UUID | None = None


class DeviceGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    group_type: str | None = None
    parent_group_id: uuid.UUID | None = None


class DeviceGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    group_type: str
    parent_group_id: uuid.UUID | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    # Computed in app.api.device_groups._to_read, not a DB column.
    device_count: int = 0
    child_group_count: int = 0


class DeviceGroupAssignRequest(BaseModel):
    device_ids: list[uuid.UUID]
