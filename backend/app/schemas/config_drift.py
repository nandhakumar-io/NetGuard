import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.config_drift import DriftSeverity


class ConfigDriftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    baseline_snapshot_id: uuid.UUID | None
    drifted: str
    severity: DriftSeverity
    lines_changed: int
    diff: str | None
    detail: str | None
    triggered_by: str
    resolved: str
    resolved_at: datetime | None
    resolved_by: str | None
    checked_at: datetime
