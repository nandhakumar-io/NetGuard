import datetime
import uuid

from pydantic import BaseModel, ConfigDict, model_validator


class AlertSnoozeCreate(BaseModel):
    device_id: uuid.UUID | None = None
    category: str | None = None
    expires_at: datetime.datetime
    reason: str | None = None

    @model_validator(mode="after")
    def _require_scope(self):
        if self.device_id is None and self.category is None:
            raise ValueError("Provide device_id, category, or both -- a snooze needs a scope.")
        return self


class AlertSnoozeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID | None = None
    device_hostname: str | None = None  # populated in app.api.alert_snoozes, not a DB column
    category: str | None = None
    reason: str | None = None
    expires_at: datetime.datetime
    created_by: str
    created_at: datetime.datetime | None = None
