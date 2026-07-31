import uuid
import datetime

from pydantic import BaseModel, ConfigDict, Field


class RunningConfigResponse(BaseModel):
    """Live read of a device's running configuration (FR: View Running Config)."""

    device_id: uuid.UUID
    hostname: str
    protocol: str
    config: str
    retrieved_at: datetime.datetime


class StartupConfigResponse(BaseModel):
    """Startup configuration for a device (FR: View Startup Config).

    Startup config isn't polled live the way running config is -- it's
    carried on the most recent ConfigSnapshot taken for the device (every
    backup captures both). `source` tells the caller whether this came
    from a snapshot or is unavailable.
    """

    device_id: uuid.UUID
    hostname: str
    config: str | None
    source: str  # "snapshot" | "unavailable"
    snapshot_id: uuid.UUID | None = None
    retrieved_at: datetime.datetime


class BackupHistoryEntry(BaseModel):
    """One entry in a device's configuration backup history (git-style log).

    Mirrors app.schemas.rollback.SnapshotSummary intentionally -- both
    represent a ConfigSnapshot row -- kept as a separate type here so the
    Configuration Management API's contract can evolve independently of
    the Rollback API's.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    change_request_id: uuid.UUID | None = None
    version: str
    checksum: str
    has_startup_config: bool = False
    created_at: datetime.datetime


class BackupConfigRequest(BaseModel):
    label: str | None = Field(default=None, description="Optional human-readable note for this backup.")


class BackupConfigResponse(BaseModel):
    snapshot: BackupHistoryEntry
    protocol: str
    message: str


class RestoreConfigRequest(BaseModel):
    snapshot_id: uuid.UUID
    reason: str | None = None


class RestoreConfigResponse(BaseModel):
    device_id: uuid.UUID
    hostname: str
    restored_from_snapshot_id: uuid.UUID
    post_restore_snapshot_id: uuid.UUID | None = None
    protocol: str
    success: bool
    message: str


class CompareConfigRequest(BaseModel):
    """Compare two configurations for a device.

    Either side may be a snapshot_id (a prior backup) or, if omitted /
    set to "live", the device's current live running configuration.
    Omitting both `base_snapshot_id` and `target_snapshot_id` compares
    the most recent backup against the live running config -- the
    common "has this device drifted since its last backup?" check.
    """

    base_snapshot_id: uuid.UUID | None = None
    target_snapshot_id: uuid.UUID | None = None


class CompareConfigResponse(BaseModel):
    device_id: uuid.UUID
    base_label: str
    target_label: str
    identical: bool
    diff: str