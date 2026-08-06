import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DriftBaseline(str, enum.Enum):
    GOLDEN_CONFIG = "golden_config"
    PREVIOUS_BACKUP = "previous_backup"
    # Compares against the shared ComplianceBaseline template for this
    # device's role (Device.device_role) instead of a per-device
    # GoldenConfig -- see drift_service._resolve_baseline_config.
    ROLE_BASELINE = "role_baseline"


class DriftSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DriftStatus(str, enum.Enum):
    OPEN = "open"
    APPROVED = "approved"
    ROLLED_BACK = "rolled_back"
    DISMISSED = "dismissed"


class ConfigDrift(Base):
    """A detected difference between a device's live running-config and a
    baseline (golden config or its own previous backup snapshot). Produced
    by app.services.drift_service.detect_drift, run nightly per-device by
    app.tasks.drift_detection_task and on-demand via
    GET /devices/{id}/drift.
    """

    __tablename__ = "config_drifts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    baseline = Column(Enum(DriftBaseline), nullable=False, default=DriftBaseline.PREVIOUS_BACKUP)
    diff_text = Column(Text, nullable=False)  # unified diff, same format as diff_engine.generate_diff
    added_lines = Column(Integer, nullable=False, default=0)
    removed_lines = Column(Integer, nullable=False, default=0)
    modified_lines = Column(Integer, nullable=False, default=0)

    risk_score = Column(Integer, nullable=False, default=0)  # 0-100, reuses risk_engine heuristics
    compliance_score = Column(Integer, nullable=False, default=100)  # 0-100, 100 = fully compliant
    severity = Column(Enum(DriftSeverity), nullable=False, default=DriftSeverity.LOW)

    ai_summary = Column(Text, nullable=True)  # human-readable findings, e.g. "ACL modified, VLAN removed"
    # Best-effort IOS-style CLI-equivalent lines derived from the
    # structural XML diff (config_format_service.xml_structural_diff +
    # to_cli_commands) -- e.g. "interface GigabitEthernet0/1" /
    # "  shutdown" instead of the raw <shutdown/> XML element. Covers the
    # common cases (interfaces, hostname, cdp); anything not specifically
    # mapped falls back to a readable "container > leaf: value" line, not
    # raw tags. Newline-joined, same convention as ai_summary/diff_text.
    cli_diff = Column(Text, nullable=True)
    status = Column(Enum(DriftStatus), nullable=False, default=DriftStatus.OPEN)

    detected_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
