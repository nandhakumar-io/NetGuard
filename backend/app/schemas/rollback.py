import uuid
import datetime

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


class RollbackResponse(BaseModel):
    change_request_id: uuid.UUID
    status: str
    message: str