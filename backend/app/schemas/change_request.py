import uuid
import datetime

from pydantic import BaseModel, ConfigDict

from app.models.change_request import ChangeStatus, ChangePriority


class ChangeRequestBase(BaseModel):
    device_id: uuid.UUID
    priority: ChangePriority = ChangePriority.MEDIUM
    description: str
    business_justification: str | None = None
    maintenance_window_start: datetime.datetime | None = None
    maintenance_window_end: datetime.datetime | None = None
    proposed_config: str


class ChangeRequestCreate(ChangeRequestBase):
    # Extra devices to deploy the same proposed_config to alongside device_id
    # (SRS 6.6 multi-device / parallel deployment). Stored JSON-encoded on
    # the model; see app.services.pipeline_service.target_device_ids.
    additional_device_ids: list[uuid.UUID] | None = None
    # Canary rollout (SRS 6.6): deploy to the first target device only,
    # wait out its health-monitoring window, and only then fan the rest of
    # the devices out. No-op when the CR targets a single device.
    canary_enabled: bool = False


class ChangeRequestRead(ChangeRequestBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submitted_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    current_config: str | None = None
    config_diff: str | None = None
    config_diff_cli: str | None = None
    config_diff_summary: str | None = None
    risk_score: int | None = None
    risk_findings: str | None = None
    validation_passed: str | None = None
    validation_errors: str | None = None
    validation_warnings: str | None = None
    status: ChangeStatus
    is_rollback: str = "false"
    rollback_snapshot_id: uuid.UUID | None = None
    risk_classification: str | None = None
    # Risk-scoring provenance (see app.models.change_request for the full
    # explanation of each): what current_config was sourced from, which
    # backend computed the score, and whether an LLM pass actually ran.
    # Drives the CR detail page's "AI-reviewed" badge and the enablement
    # of the rescore/retry action.
    config_source: str | None = None
    risk_engine_backend: str | None = None
    risk_llm_applied: bool = False
    risk_llm_error: str | None = None
    # Critical Risk changes require two distinct Network Administrator
    # approvals before deployment is enqueued (SRS 6.2 / FR-6).
    requires_dual_approval: bool = False
    dual_approval_reason: str | None = None
    first_approved_by: uuid.UUID | None = None
    canary_enabled: bool = False
    created_at: datetime.datetime


class RiskAnalysisResult(BaseModel):
    risk_score: int
    classification: str  # Low Risk | Medium Risk | Critical Risk
    recommendation: str
    findings: list[str]
    # True only when RISK_ENGINE_BACKEND == "llm" AND the model call
    # actually succeeded and its findings were merged in -- not merely
    # that the "llm" backend was selected (a silent fallback to the
    # rule-based score, e.g. Ollama unreachable, leaves this False). This
    # is what the "AI-reviewed" badge and the rescore/retry flow key off.
    llm_applied: bool = False
    # Set when RISK_ENGINE_BACKEND == "llm" but llm_applied is False --
    # why the call didn't happen or didn't succeed (no credential/server
    # unreachable/bad response), so a reviewer knows *why* before hitting
    # retry, and the retry action has something concrete to act on.
    llm_error: str | None = None


class ValidationReport(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]