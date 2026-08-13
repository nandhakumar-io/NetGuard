import datetime
import uuid

from pydantic import BaseModel, ConfigDict, model_validator


class RecurringMaintenanceScheduleBase(BaseModel):
    name: str
    reason: str | None = None
    scope: str = "device"  # device | site | fleet
    device_id: uuid.UUID | None = None
    site: str | None = None

    frequency: str  # weekly | monthly
    interval: int = 1
    day_of_week: int  # 0=Monday .. 6=Sunday
    week_of_month: int | None = None  # required for monthly: 1-4 or -1 (last)

    start_time: datetime.time
    duration_minutes: int = 120

    active_from: datetime.datetime
    active_until: datetime.datetime | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _validate(self):
        if self.scope == "device" and not self.device_id:
            raise ValueError("device_id is required for a device-scoped schedule")
        if self.scope == "site" and not self.site:
            raise ValueError("site is required for a site-scoped schedule")
        if not (0 <= self.day_of_week <= 6):
            raise ValueError("day_of_week must be 0 (Monday) through 6 (Sunday)")
        if self.frequency == "monthly" and self.week_of_month is None:
            raise ValueError("week_of_month is required for a monthly schedule (1-4, or -1 for last)")
        if self.week_of_month is not None and not (self.week_of_month == -1 or 1 <= self.week_of_month <= 4):
            raise ValueError("week_of_month must be 1-4, or -1 for 'last'")
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        if self.active_until and self.active_until <= self.active_from:
            raise ValueError("active_until must be after active_from")
        return self


class RecurringMaintenanceScheduleCreate(RecurringMaintenanceScheduleBase):
    pass


class RecurringMaintenanceScheduleUpdate(BaseModel):
    name: str | None = None
    reason: str | None = None
    active_until: datetime.datetime | None = None
    enabled: bool | None = None


class RecurringMaintenanceScheduleRead(RecurringMaintenanceScheduleBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_by: str
    created_at: datetime.datetime
    updated_at: datetime.datetime | None = None


class ScheduleOccurrencePreview(BaseModel):
    occurrences: list[datetime.datetime]
