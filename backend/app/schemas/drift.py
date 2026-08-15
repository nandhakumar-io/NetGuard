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
    maintenance_window_id: uuid.UUID | None = None


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


class WeeklyGoldenDriftGroup(BaseModel):
    """One device-group's slice of the weekly digest -- e.g. "Edge
    Firewalls" or "Branch Routers - East Region" (app.models.device_group.
    DeviceGroup). group_id is None for the "Ungrouped" bucket (devices with
    no DeviceGroup assigned)."""

    group_id: uuid.UUID | None
    group_name: str
    devices: list[WeeklyGoldenDriftEntry]


class WeeklyGoldenDriftReport(BaseModel):
    since: datetime.datetime
    days: int
    devices: list[WeeklyGoldenDriftEntry]
    # Same devices as `devices` above, bucketed by DeviceGroup -- the NOC
    # digest view groups by fleet/team ownership rather than showing one
    # long flat table. `devices` is kept for backward compatibility /
    # callers that just want the flat list.
    groups: list[WeeklyGoldenDriftGroup]


class LowRiskDriftCandidate(DriftRead):
    """An OPEN, LOW-severity drift where every changed line is a cosmetic
    description/remark edit -- eligible for one-click bulk approval via
    POST /drift/bulk-approve. See drift_service.is_low_risk_bulk_approvable."""

    hostname: str


class BulkApproveRequest(BaseModel):
    # None = approve every current low-risk candidate fleet-wide (same set
    # GET /drift/low-risk-candidates returns). Pass explicit IDs to approve
    # a hand-picked subset instead (e.g. after the user deselected a few
    # rows in the preview table).
    drift_ids: list[uuid.UUID] | None = None


class BulkApproveResponse(BaseModel):
    approved_count: int
    approved_ids: list[uuid.UUID]
    # Requested (via drift_ids) but not eligible -- already reviewed, or no
    # longer qualifies as low-risk-cosmetic. Never populated when drift_ids
    # is omitted, since the candidate set is by definition all-eligible.
    skipped_ids: list[uuid.UUID]


class DriftTrendPoint(BaseModel):
    bucket_start: datetime.date
    total: int
    critical: int
    high: int
    distinct_devices: int


class DriftTrendResponse(BaseModel):
    days: int
    bucket_days: int
    points: list[DriftTrendPoint]


class FlappingDeviceEntry(BaseModel):
    device_id: uuid.UUID
    hostname: str
    event_count: int
    last_detected_at: datetime.datetime
    max_severity: DriftSeverity


class FlappingDevicesResponse(BaseModel):
    days: int
    min_events: int
    devices: list[FlappingDeviceEntry]
