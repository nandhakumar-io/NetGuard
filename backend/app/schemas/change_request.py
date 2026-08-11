import datetime
import json
import uuid

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.models.change_request import ChangePriority, ChangeStatus


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
    # Auto-link (postmortem traceability): set when this CR is being
    # submitted directly from an Alert Center alert as its remediation.
    # See app.models.change_request.ChangeRequest.triggering_alert_id.
    alert_id: uuid.UUID | None = None


class BlastRadiusPreview(BaseModel):
    """Pre-deployment blast-radius preview (see
    app.services.topology_service.compute_blast_radius): how many devices
    this change touches directly, how many of those are core, and how
    many *other* devices depend on the touched ones via topology -- shown
    before a change is pushed so a risky-looking fan-out gets a second
    look, not just a risk score on the diff content itself.
    """

    touched_count: int
    touched_core_count: int
    touched_roles: dict[str, int]
    touched_device_ids: list[uuid.UUID]
    dependent_count: int
    dependent_device_ids: list[uuid.UUID]
    unknown_device_ids: list[uuid.UUID] = []


class ImpactSimulationRequest(BaseModel):
    """Pre-submission dry-run input: mirrors ChangeRequestCreate's device_id
    + proposed_config, without needing a change request to already exist."""

    device_id: uuid.UUID
    proposed_config: str


class RemovedLinkPreview(BaseModel):
    interface: str
    reason: str
    neighbor_device_id: uuid.UUID | None = None
    neighbor_hostname: str | None = None
    neighbor_port: str | None = None


class DeviceImpactPreview(BaseModel):
    device_id: uuid.UUID
    hostname: str
    device_role: str | None = None
    before_hop_count: int
    after_hop_count: int | None = None
    status: str


class ImpactSimulationPreview(BaseModel):
    """Pre-deployment "what-if" dry run (see
    app.services.impact_simulation_service.simulate_impact): simulates the
    proposed config's effect on the live topology graph -- which confirmed
    links would go down, and whether every device on the far side of them
    still has a redundant path -- before the change is ever pushed to a
    device.
    """

    device_id: uuid.UUID
    hostname: str
    affected_interfaces: list[str] = []
    removed_links: list[RemovedLinkPreview] = []
    isolated_devices: list[DeviceImpactPreview] = []
    degraded_devices: list[DeviceImpactPreview] = []
    reachable_unaffected_count: int = 0
    total_dependent_count: int = 0
    classification: str
    summary: str


class TriggeringAlertSummary(BaseModel):
    """Minimal alert info embedded on ChangeRequestRead for the postmortem
    link (full alert detail lives on GET /alerts/{id} / Alert Center)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    severity: str
    category: str
    message: str
    created_at: datetime.datetime


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
    # Parsed from the model's JSON-encoded Text column (see
    # app.models.change_request.ChangeRequest.additional_device_ids) --
    # the extra devices this CR fans out to alongside device_id (SRS 6.6
    # bulk/multi-device deploy). Was write-only before: the create schema
    # accepted it but ChangeRequestRead never gave it back, so the UI had
    # no way to show "this CR targets N devices" or which ones after
    # submission -- see the field_validator below for the JSON parse.
    additional_device_ids: list[uuid.UUID] = []
    # 1 + len(additional_device_ids), computed rather than stored, purely
    # so the frontend list/detail views don't have to reimplement that
    # arithmetic themselves.
    target_device_count: int = 1
    created_at: datetime.datetime

    # Approval workflow visibility (who approved, SLA on pending queue):
    # approved_at is the final approval's timestamp (see model docstring).
    # The *_name fields are display-only conveniences resolved by
    # app.api.change_requests._hydrate so the UI doesn't have to issue a
    # separate /users lookup per CR just to show who submitted/approved.
    approved_at: datetime.datetime | None = None
    submitted_by_name: str | None = None
    approved_by_name: str | None = None
    first_approved_by_name: str | None = None

    # Alert -> CR auto-link (postmortem traceability). None for changes
    # not raised from a specific alert.
    triggering_alert_id: uuid.UUID | None = None
    triggering_alert: TriggeringAlertSummary | None = None

    @field_validator("additional_device_ids", mode="before")
    @classmethod
    def _parse_additional_device_ids(cls, v: object) -> list:
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return []
        return v

    @model_validator(mode="after")
    def _compute_target_device_count(self) -> "ChangeRequestRead":
        self.target_device_count = 1 + len(self.additional_device_ids)
        return self


class PendingApprovalItem(BaseModel):
    """One row of GET /change-requests/pending-approvals: a CR plus its
    SLA timer, so the approval queue can be sorted/highlighted by how
    close (or past) each request is to breaching its priority's SLA
    without the frontend re-deriving the threshold table itself."""

    change_request: ChangeRequestRead
    sla_hours: float
    elapsed_hours: float
    due_at: datetime.datetime
    is_overdue: bool
    is_first_approval_needed: bool


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
