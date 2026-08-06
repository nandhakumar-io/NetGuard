import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ChangeStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    VALIDATING = "validating"
    DEPLOYING = "deploying"
    MONITORING = "monitoring"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ChangePriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    # JSON-encoded list of extra device UUIDs (SRS 6.6 multi-device deployment).
    # Stored as text rather than a join table to keep the prototype schema
    # simple; see pipeline_service.target_device_ids() for the reader side.
    additional_device_ids = Column(Text, nullable=True)
    submitted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    priority = Column(Enum(ChangePriority), nullable=False, default=ChangePriority.MEDIUM)
    description = Column(Text, nullable=False)
    business_justification = Column(Text, nullable=True)
    maintenance_window_start = Column(DateTime(timezone=True), nullable=True)
    maintenance_window_end = Column(DateTime(timezone=True), nullable=True)

    current_config = Column(Text, nullable=True)
    proposed_config = Column(Text, nullable=False)
    config_diff = Column(Text, nullable=True)
    # Structural, human-readable companions to the raw config_diff above
    # (see config_format_service.xml_structural_diff) -- config_diff_cli
    # is best-effort IOS CLI-equivalent lines, config_diff_summary is a
    # plain-English one-line-per-change list. Both None when either side
    # of the diff isn't XML (plain CLI-sourced config diffs just fine as
    # raw text already) or the structural diff couldn't be computed.
    config_diff_cli = Column(Text, nullable=True)
    config_diff_summary = Column(Text, nullable=True)

    risk_score = Column(Integer, nullable=True)  # 0-100, set by AI Configuration Analyzer
    risk_findings = Column(Text, nullable=True)  # JSON-encoded list of detected risks
    risk_classification = Column(String, nullable=True)  # Low Risk | Medium Risk | Critical Risk

    # Where current_config came from: "live" (a fresh read off the device
    # right before scoring), "snapshot" (the live read failed/wasn't
    # possible, fell back to the last ConfigSnapshot on file), or "none"
    # (neither -- first-ever config for this device). Surfaced on the CR
    # detail page so a reviewer knows how fresh the "current" side of the
    # diff/risk analysis actually was, and is what the retry/rescore
    # action (POST /change-requests/{id}/rescore) tries to upgrade from
    # "snapshot"/"none" to "live".
    config_source = Column(String, nullable=True)
    # settings.RISK_ENGINE_BACKEND ("rules" | "llm") at the time this CR's
    # risk score was last computed -- NOT the same as risk_llm_applied
    # below: backend can be "llm" while the call still fell back silently
    # (no credential / provider unreachable). Kept alongside risk_llm_applied
    # so the CR detail page can show *both* "LLM backend was selected" and
    # "an LLM pass actually ran", rather than conflating the two.
    risk_engine_backend = Column(String, nullable=True)
    # True only when risk_engine_backend == "llm" AND the model call
    # actually succeeded and its findings were merged into risk_score/
    # risk_findings -- what the "AI-reviewed" badge is keyed off.
    risk_llm_applied = Column(Boolean, nullable=False, default=False, server_default="false")
    # Why risk_llm_applied is False despite backend == "llm" (no credential,
    # provider unreachable, bad response, ...). None when llm_applied is
    # True, or when the backend was never "llm" in the first place. This is
    # what the retry/rescore action shows a reviewer before they click it.
    risk_llm_error = Column(Text, nullable=True)

    # Critical Risk dual approval (SRS 6.2 / FR-6): when true, a single
    # approve() call is not enough -- the first call records
    # first_approved_by/first_approved_at and leaves status unchanged; a
    # *different* Network Administrator must approve again to finalize.
    requires_dual_approval = Column(Boolean, nullable=False, default=False, server_default="false")
    # Why requires_dual_approval is set: "Critical Risk", "Blast Radius", or
    # "Critical Risk + Blast Radius" when both independently trigger it (see
    # app.api.change_requests.create_change_request). Purely informational
    # for the audit trail / UI -- the approve() gate itself only checks
    # requires_dual_approval.
    dual_approval_reason = Column(String, nullable=True)
    first_approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    first_approved_at = Column(DateTime(timezone=True), nullable=True)

    # Canary multi-device deploy (SRS 6.6): when true and this CR targets
    # more than one device, the first target device deploys and clears its
    # health-monitoring window before the rest of the fleet is touched at
    # all. See app.tasks.run_deployment_pipeline_task / canary_gate_task.
    canary_enabled = Column(Boolean, nullable=False, default=False, server_default="false")

    # Automated Validation Engine (SRS 6.4 / FR-5): result of the last
    # validation_engine.validate_syntax() run against this CR's
    # proposed_config. Re-checked (and refreshed) at both submission and
    # approval time -- see app.api.change_requests -- so this always
    # reflects the most recent validation, not just the one at creation.
    validation_passed = Column(String, nullable=True)  # "true" | "false"
    validation_errors = Column(Text, nullable=True)  # JSON-encoded list[str]
    validation_warnings = Column(Text, nullable=True)  # JSON-encoded list[str]

    status = Column(Enum(ChangeStatus), nullable=False, default=ChangeStatus.DRAFT)

    # Change Management & Rollback: a manual rollback is modeled as a
    # regular change request whose proposed_config is a prior snapshot's
    # config, so it runs through the exact same Snapshot -> Deploy ->
    # Health Monitor pipeline as any other change (see
    # app.services.rollback_service). These two columns are what
    # distinguish it from an engineer-authored change and record which
    # snapshot it's restoring, for the audit trail / UI.
    is_rollback = Column(String, nullable=False, default="false", server_default="false")
    rollback_snapshot_id = Column(UUID(as_uuid=True), ForeignKey("config_snapshots.id"), nullable=True)

    # Alert -> Change Request auto-link (postmortem traceability): set when
    # a CR is submitted directly from an Alert Center alert (see
    # ChangeRequestCreate.alert_id / app.api.change_requests.create_change_request).
    # Lets a reviewer jump from "what fixed this incident" on the alert
    # straight to the CR that was raised for it, and vice versa (GET
    # /change-requests?alert_id=...). None for changes not tied to any
    # specific alert -- the common case.
    triggering_alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=True, index=True)

    # Approval workflow visibility (who/when, SLA timers on the pending
    # queue): approved_at is the timestamp of the *final* approval that
    # moved this CR to APPROVED -- for a dual-approval CR that's the
    # second approval; first_approved_at above already covers the first.
    # updated_at isn't reliable for this since later pipeline stages
    # (deploying/monitoring/success) also touch it.
    approved_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
