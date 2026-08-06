import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FirmwareUpgradeCreate(BaseModel):
    device_id: uuid.UUID
    target_version: str
    image_filename: str
    image_sha256: str | None = None
    maintenance_window_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    reboot_wait_seconds: int = 90


class FirmwareUpgradeBatchCreate(BaseModel):
    """Bulk upgrade: same target image across many devices at once (the
    SolarWinds-NCM-style workflow), represented as one FirmwareUpgrade row
    per device sharing a batch_id so each device's progress and any
    failure/rollback is independent and individually retryable.
    """

    device_ids: list[uuid.UUID]
    target_version: str
    image_filename: str
    image_sha256: str | None = None
    maintenance_window_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    reboot_wait_seconds: int = 90


class FirmwareUpgradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_id: uuid.UUID | None = None
    device_id: uuid.UUID
    from_version: str | None = None
    target_version: str
    image_filename: str
    image_sha256: str | None = None
    status: str
    current_step_detail: str | None = None
    error_message: str | None = None
    maintenance_window_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    pre_upgrade_snapshot_id: uuid.UUID | None = None
    reboot_wait_seconds: int
    attempts: int
    initiated_by: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
