import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.config_drift import DriftBaseline, DriftSeverity, DriftStatus


class DriftScanRequest(BaseModel):
    baseline: DriftBaseline = DriftBaseline.PREVIOUS_BACKUP


class DriftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    baseline: DriftBaseline
    added_lines: int
    removed_lines: int
    modified_lines: int
    risk_score: int
    compliance_score: int
    severity: DriftSeverity
    ai_summary: str | None = None
    status: DriftStatus
    detected_at: datetime.datetime


class DriftDetail(DriftRead):
    diff_text: str
    cli_diff: str | None = None


class DriftScanResponse(BaseModel):
    drift: DriftDetail
    baseline_label: str
    findings: list[str]
    rollback_recommendation: dict


class RollbackRecommendationResponse(BaseModel):
    recommended: bool
    reason: str


class DriftFleetSummary(BaseModel):
    total_open_drifts: int
    devices_drifted: int
    average_compliance_score: int
    by_severity: dict[str, int]
    rollback_recommended_count: int


class DriftStatusUpdate(BaseModel):
    status: DriftStatus


class WeeklyGoldenDriftEntry(DriftRead):
    hostname: str


class WeeklyGoldenDriftReport(BaseModel):
    since: datetime.datetime
    days: int
    devices: list[WeeklyGoldenDriftEntry]
