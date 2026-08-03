"""Configuration Drift Detection Service.

Detects when a device's live running configuration has diverged from a
baseline -- either its own last-known-good ConfigSnapshot
(DriftBaseline.PREVIOUS_BACKUP) or an explicitly approved GoldenConfig
(DriftBaseline.GOLDEN_CONFIG) -- and records the result as a ConfigDrift
row.

Reuses existing building blocks rather than duplicating them:
  - app.services.protocol_manager.ProtocolManager for the live config read
    (NETCONF > RESTCONF > SSH, same as everywhere else)
  - app.services.diff_engine.generate_diff for the unified diff
  - app.services.risk_engine.analyze for risk scoring (the same weighted
    rule set used to score a proposed change is used here to score how
    risky the *drifted* config is)
  - app.services.snapshot_service.decrypt_config to read baseline configs
  - app.services.audit_service.record_event / app.services.event_bus for
    the same audit-trail + live-dashboard visibility every other action gets
  - app.services.notification_service for HIGH/CRITICAL drift alerts

Called by:
  - app.api.drift (on-demand scan, GET .../drift/scan)
  - app.tasks.drift_detection_task (nightly per-device Celery task, see
    celery beat schedule in app.celery_app)
"""
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSource
from app.models.config_drift import ConfigDrift, DriftBaseline, DriftSeverity, DriftStatus
from app.models.device import Device
from app.models.golden_config import GoldenConfig
from app.models.snapshot import ConfigSnapshot
from app.services import alert_service, audit_service, diff_engine, event_bus, notification_service, risk_engine, snapshot_service
from app.services.protocol_manager import ProtocolManager

# Compliance score starts at 100 (fully compliant) and is docked per
# changed line plus a flat penalty for anything the risk engine flags,
# since a risky change is a compliance concern even if it's only one line.
COMPLIANCE_PENALTY_PER_LINE = 3
COMPLIANCE_PENALTY_PER_RISK_FINDING = 10
COMPLIANCE_FLOOR = 0

# Severity bands, driven off the higher of (risk_score, 100 - compliance_score)
# so a config that's drifted a lot but is low-risk still surfaces as at
# least MEDIUM, and a single high-risk line (e.g. a removed BGP neighbor)
# still surfaces as CRITICAL even if only one line changed.
SEVERITY_THRESHOLDS: list[tuple[int, DriftSeverity]] = [
    (70, DriftSeverity.CRITICAL),
    (40, DriftSeverity.HIGH),
    (15, DriftSeverity.MEDIUM),
    (0, DriftSeverity.LOW),
]

# Drift at/above this severity generates an Alert + notification, same
# threshold philosophy as the risk engine's own low/medium/critical split.
ALERTING_SEVERITIES = (DriftSeverity.HIGH, DriftSeverity.CRITICAL)


class NoBaselineError(Exception):
    """Raised when the requested baseline doesn't exist yet for this device
    (no golden config set, or no prior snapshot taken)."""


@dataclass
class DriftDetectionResult:
    drift: ConfigDrift
    baseline_label: str
    live_config: str
    baseline_config: str
    alert: Alert | None = None
    findings: list[str] = field(default_factory=list)


def _classify_severity(risk_score: int, compliance_score: int) -> DriftSeverity:
    signal = max(risk_score, 100 - compliance_score)
    for threshold, severity in SEVERITY_THRESHOLDS:
        if signal >= threshold:
            return severity
    return DriftSeverity.LOW


def _count_diff_lines(diff_text: str) -> tuple[int, int, int]:
    """Returns (added, removed, modified) from a unified diff produced by
    diff_engine.generate_diff. "Modified" is approximated as
    min(added, removed) -- a changed line shows up as one removed + one
    added in a unified diff, so pairing them up gives a reasonable count
    of lines that were altered rather than purely added or purely removed.
    """
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    modified = min(added, removed)
    return added, removed, modified


def _resolve_baseline_config(db: Session, device: Device, baseline: DriftBaseline) -> tuple[str, str]:
    """Returns (label, decrypted_config_text) for the requested baseline."""
    if baseline == DriftBaseline.GOLDEN_CONFIG:
        golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
        if golden is None:
            raise NoBaselineError(
                f"No golden config has been set for '{device.hostname}' yet. "
                "Set one via Configuration Management, or scan against the previous backup instead."
            )
        return "golden config", snapshot_service.decrypt_config(golden.config_encrypted)

    latest_snapshot = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device.id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )
    if latest_snapshot is None:
        raise NoBaselineError(
            f"No configuration backup exists yet for '{device.hostname}' to compare against. "
            "Take a backup first (POST /devices/{id}/config/backup)."
        )
    return f"backup v{latest_snapshot.version}", snapshot_service.decrypt_config(
        latest_snapshot.running_config_encrypted
    )


def _build_ai_summary(findings: list[str], added: int, removed: int) -> str:
    if not findings or findings == ["No significant risk patterns detected"]:
        if added == 0 and removed == 0:
            return "No drift detected. Live configuration matches baseline."
        return f"{added} line(s) added, {removed} line(s) removed. No high-risk patterns detected."
    return "; ".join(findings)


def _rollback_recommended(severity: DriftSeverity, compliance_score: int) -> bool:
    return severity in (DriftSeverity.HIGH, DriftSeverity.CRITICAL) or compliance_score < 60


def detect_drift(
    db: Session,
    device: Device,
    baseline: DriftBaseline = DriftBaseline.PREVIOUS_BACKUP,
    triggered_by: str = "system",
) -> DriftDetectionResult:
    """Runs one drift check for `device` against `baseline` and persists a
    ConfigDrift row. Raises NoBaselineError if the baseline doesn't exist
    yet (no golden config set / no prior backup) -- callers decide whether
    that's a 404, a skip, or worth surfacing to the user.
    """
    baseline_label, baseline_config = _resolve_baseline_config(db, device, baseline)

    pm = ProtocolManager(db, device, operator=triggered_by)
    live_result = pm.get_running_config()
    if not live_result.success:
        raise RuntimeError(live_result.error or f"Failed to read live running configuration from {device.hostname}")
    live_config = live_result.output

    diff_text = diff_engine.generate_diff(baseline_config, live_config)
    added, removed, modified = _count_diff_lines(diff_text)

    risk = risk_engine.analyze(live_config, baseline_config)
    line_penalty = min((added + removed) * COMPLIANCE_PENALTY_PER_LINE, 60)
    finding_penalty = 0
    if risk.findings and risk.findings != ["No significant risk patterns detected"]:
        finding_penalty = len(risk.findings) * COMPLIANCE_PENALTY_PER_RISK_FINDING
    compliance_score = max(COMPLIANCE_FLOOR, 100 - line_penalty - finding_penalty)

    severity = _classify_severity(risk.risk_score, compliance_score)
    ai_summary = _build_ai_summary(risk.findings, added, removed)

    drift = ConfigDrift(
        device_id=device.id,
        baseline=baseline,
        diff_text=diff_text,
        added_lines=added,
        removed_lines=removed,
        modified_lines=modified,
        risk_score=risk.risk_score,
        compliance_score=compliance_score,
        severity=severity,
        ai_summary=ai_summary,
        status=DriftStatus.OPEN,
    )
    db.add(drift)
    db.commit()
    db.refresh(drift)

    audit_service.record_event(
        db,
        actor=triggered_by,
        action="Drift Detected" if (added or removed) else "Drift Scan (no changes)",
        result=severity.value,
        device_hostname=device.hostname,
        detail=(
            f"baseline={baseline_label} +{added}/-{removed} lines "
            f"risk={risk.risk_score} compliance={compliance_score}"
        ),
    )

    alert = None
    if severity in ALERTING_SEVERITIES and (added or removed):
        # Dedup-aware: a device that keeps drifting the same way on every
        # scheduled sweep updates one standing alert instead of piling up
        # a fresh row every sweep (see alert_service.raise_alert).
        alert, is_new = alert_service.raise_alert(
            db,
            device_id=device.id,
            severity="critical" if severity == DriftSeverity.CRITICAL else "warning",
            source=AlertSource.DRIFT,
            category=f"Configuration Drift ({severity.value.title()})",
            message=(
                f"{device.hostname} has drifted from its {baseline_label}: "
                f"{ai_summary} (compliance {compliance_score}/100, risk {risk.risk_score}/100)."
            ),
        )

        if is_new:
            notification_service.notify(
                event="Configuration Drift Detected",
                message=alert.message,
                severity="critical" if severity == DriftSeverity.CRITICAL else "warning",
                device_hostname=device.hostname,
            )

    event_bus.publish_event(
        "drift_detected",
        device_id=str(device.id),
        drift_id=str(drift.id),
        severity=severity.value,
        compliance_score=compliance_score,
    )

    return DriftDetectionResult(
        drift=drift,
        baseline_label=baseline_label,
        live_config=live_config,
        baseline_config=baseline_config,
        alert=alert,
        findings=risk.findings,
    )


def rollback_recommendation(drift: ConfigDrift) -> dict:
    """Advisory recommendation surfaced on the Drift Page -- does not take
    any action itself. The actual rollback is triggered by the caller via
    POST /devices/{id}/rollback (app.services.rollback_service), reusing
    the same snapshot -> deploy -> health-monitor pipeline as any other
    rollback rather than a drift-specific shortcut.
    """
    recommended = _rollback_recommended(drift.severity, drift.compliance_score)
    if not recommended:
        return {
            "recommended": False,
            "reason": "Drift severity and compliance impact are within acceptable range. Monitor only.",
        }
    if drift.baseline == DriftBaseline.PREVIOUS_BACKUP:
        return {
            "recommended": True,
            "reason": (
                f"{drift.severity.value.title()} severity drift with compliance score "
                f"{drift.compliance_score}/100. Roll the device back to its last known-good backup."
            ),
        }
    return {
        "recommended": True,
        "reason": (
            f"{drift.severity.value.title()} severity drift from the approved golden config "
            f"(compliance {drift.compliance_score}/100). Restore the golden config or take a new "
            "backup and roll back to it once reviewed."
        ),
    }


def list_drifts(
    db: Session,
    device_id: uuid.UUID | None = None,
    status: DriftStatus | None = None,
    severity: DriftSeverity | None = None,
    limit: int = 100,
) -> list[ConfigDrift]:
    query = db.query(ConfigDrift)
    if device_id is not None:
        query = query.filter(ConfigDrift.device_id == device_id)
    if status is not None:
        query = query.filter(ConfigDrift.status == status)
    if severity is not None:
        query = query.filter(ConfigDrift.severity == severity)
    return query.order_by(ConfigDrift.detected_at.desc()).limit(limit).all()


def fleet_summary(db: Session) -> dict:
    """Powers the Drift Dashboard Widget: fleet-wide drift posture at a glance."""
    open_drifts = db.query(ConfigDrift).filter(ConfigDrift.status == DriftStatus.OPEN).all()
    total_open = len(open_drifts)
    by_severity = {s.value: 0 for s in DriftSeverity}
    for d in open_drifts:
        by_severity[d.severity.value] += 1

    avg_compliance = (
        round(sum(d.compliance_score for d in open_drifts) / total_open) if total_open else 100
    )
    devices_drifted = len({d.device_id for d in open_drifts})

    return {
        "total_open_drifts": total_open,
        "devices_drifted": devices_drifted,
        "average_compliance_score": avg_compliance,
        "by_severity": by_severity,
        "rollback_recommended_count": sum(
            1 for d in open_drifts if _rollback_recommended(d.severity, d.compliance_score)
        ),
    }