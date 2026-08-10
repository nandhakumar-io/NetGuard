import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class SnapshotSummary(BaseModel):
    """A device's config version history entry. Never includes the
    encrypted config payload itself -- only enough to identify and pick a
    version to roll back to (git-style log, not git-style checkout).
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    change_request_id: uuid.UUID | None = None
    version: str
    checksum: str
    created_at: datetime.datetime


class RollbackRequest(BaseModel):
    snapshot_id: uuid.UUID
    reason: str | None = None


class RollbackPreviewResponse(BaseModel):
    """Read-only diff shown before a rollback is confirmed -- no
    ChangeRequest is created and nothing is pushed to the device just by
    requesting this."""

    device_id: uuid.UUID
    snapshot_id: uuid.UUID
    target_version: str
    current_source: str  # "live" | "last_snapshot" | "unavailable"
    diff: str
    identical: bool
    added_lines: int
    removed_lines: int
    warning: str | None = None
    blocked: bool = False
    blocked_reason: str | None = None


class RollbackResponse(BaseModel):
    change_request_id: uuid.UUID
    status: str
    message: str


class RollbackSection(BaseModel):
    """One independently revertible block found in a device's current
    config (an ACL, VLAN, interface stanza, etc) -- the picklist for a
    section-level (partial) rollback."""
    key: str
    kind: str
    name: str
    line_count: int


class PartialRollbackRequest(BaseModel):
    snapshot_id: uuid.UUID
    section_key: str
    reason: str | None = None


class PartialRollbackPreviewResponse(BaseModel):
    device_id: uuid.UUID
    snapshot_id: uuid.UUID
    section_key: str
    section: dict
    current_source: str
    diff: str
    identical: bool
    blocked: bool = False
    blocked_reason: str | None = None
