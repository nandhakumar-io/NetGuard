"""Configuration Drift Detection Service.

Detects when a device's live running configuration has diverged from a
baseline -- its own last-known-good ConfigSnapshot
(DriftBaseline.PREVIOUS_BACKUP), an explicitly approved per-device
GoldenConfig (DriftBaseline.GOLDEN_CONFIG), or the shared compliance
template for its role (DriftBaseline.ROLE_BASELINE, via
ComplianceBaseline + Device.device_role -- a core switch and an access
switch shouldn't be judged against the same baseline just because
nobody's set a per-device GoldenConfig for either of them) -- and
records the result as a ConfigDrift row.

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
import datetime
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.change_request import ChangeRequest
    from app.models.user import User

from app.models.alert import Alert, AlertSource
from app.models.compliance_baseline import ComplianceBaseline
from app.models.config_drift import (
    ConfigDrift,
    DriftBaseline,
    DriftSeverity,
    DriftStatus,
)
from app.models.device import Device
from app.models.golden_config import GoldenConfig
from app.models.snapshot import ConfigSnapshot
from app.services import (
    alert_service,
    audit_service,
    diff_engine,
    event_bus,
    notification_service,
    risk_engine,
    snapshot_service,
)
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

    if baseline == DriftBaseline.ROLE_BASELINE:
        if not device.device_role:
            raise NoBaselineError(
                f"'{device.hostname}' has no device_role set, so there's no role to look up a "
                "compliance baseline for. Set one via PATCH /devices/{id} (device_role), or scan "
                "against the golden config / previous backup instead."
            )
        role_baseline = (
            db.query(ComplianceBaseline)
            .filter(ComplianceBaseline.device_role == device.device_role)
            .first()
        )
        if role_baseline is None:
            raise NoBaselineError(
                f"No compliance baseline has been set for the '{device.device_role}' role yet "
                f"(device '{device.hostname}'). Set one via PUT /compliance-baselines/{device.device_role}."
            )
        return (
            f"role baseline ({device.device_role})",
            snapshot_service.decrypt_config(role_baseline.config_encrypted),
        )

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

    drift_analysis = risk_engine.analyze_drift(live_config, baseline_config, diff_text, added, removed)
    line_penalty = min((added + removed) * COMPLIANCE_PENALTY_PER_LINE, 60)
    finding_penalty = 0
    if drift_analysis.findings and drift_analysis.findings != ["No significant risk patterns detected"]:
        finding_penalty = len(drift_analysis.findings) * COMPLIANCE_PENALTY_PER_RISK_FINDING
    compliance_score = max(COMPLIANCE_FLOOR, 100 - line_penalty - finding_penalty)

    severity = _classify_severity(drift_analysis.risk_score, compliance_score)
    ai_summary = drift_analysis.ai_summary

    drift = ConfigDrift(
        device_id=device.id,
        baseline=baseline,
        diff_text=diff_text,
        added_lines=added,
        removed_lines=removed,
        modified_lines=modified,
        risk_score=drift_analysis.risk_score,
        compliance_score=compliance_score,
        severity=severity,
        ai_summary=ai_summary,
        cli_diff="\n".join(drift_analysis.cli_diff) if drift_analysis.cli_diff else None,
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
            f"risk={drift_analysis.risk_score} compliance={compliance_score} "
            f"ai_summary={'llm' if drift_analysis.llm_applied else 'rules-fallback'}"
            + (f" llm_error={drift_analysis.llm_error}" if drift_analysis.llm_error else "")
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
                f"{ai_summary} (compliance {compliance_score}/100, risk {drift_analysis.risk_score}/100)."
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
        findings=drift_analysis.findings,
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


class RemediationError(Exception):
    """Raised when a drift can't be auto-remediated as requested (wrong
    baseline type, device busy with another change, etc)."""


def remediate_drift(db: Session, drift: ConfigDrift, device: Device, actor: "User") -> "ChangeRequest":
    """One-click "push golden config to fix drift": builds and auto-approves
    a ChangeRequest that redeploys the drift's own baseline config back to
    the device, then queues it on the standard Snapshot -> Deploy -> Health
    Monitor -> Success/Self-Healing-Rollback pipeline -- same safety net as
    any other change, not a drift-specific shortcut that skips validation
    or health checks.

    Only meaningful for GOLDEN_CONFIG and ROLE_BASELINE drift: those
    baselines are an intentional "this is what should be running" target
    a NetGuard operator has explicitly approved. PREVIOUS_BACKUP drift has
    no such intentional target -- the old snapshot is just whatever was
    running before, not necessarily correct -- so that case is directed to
    the existing snapshot-picker rollback flow (POST /devices/{id}/rollback)
    instead of being silently reinterpreted as "restore the old backup".
    """
    from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
    from app.services import credential_service, deployment_engine

    if drift.status != DriftStatus.OPEN:
        raise RemediationError(
            f"Drift {drift.id} is already '{drift.status.value}' -- nothing to remediate."
        )

    if drift.baseline not in (DriftBaseline.GOLDEN_CONFIG, DriftBaseline.ROLE_BASELINE):
        raise RemediationError(
            "Auto-remediation pushes an approved baseline config (golden config or role baseline) "
            "back to the device. This drift was detected against the device's previous backup, which "
            "isn't an approved target to push -- use POST /devices/{id}/rollback to pick a specific "
            "backup snapshot to restore instead."
        )

    baseline_label, baseline_config = _resolve_baseline_config(db, device, drift.baseline)

    in_flight = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.device_id == device.id,
            ChangeRequest.status.in_(
                (ChangeStatus.APPROVED, ChangeStatus.DEPLOYING, ChangeStatus.MONITORING)
            ),
        )
        .first()
    )
    if in_flight is not None:
        raise RemediationError(
            f"Device '{device.hostname}' already has change request {in_flight.id} in status "
            f"'{in_flight.status.value}'. Wait for it to finish before auto-remediating this drift."
        )

    # Best-effort live read for the audit-trail/diff "before" side; a
    # failed read never blocks remediation -- it just falls back to the
    # drift record's own captured live_config-less diff_text context.
    current_config = None
    try:
        ssh_password = credential_service.get_ssh_password(device)
        from app.services.pipeline_service import DEVICE_TYPE_MAP

        netmiko_type = DEVICE_TYPE_MAP.get(
            device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios"
        )
        current_config, _ = deployment_engine.read_running_config(
            netmiko_type, device.ip_address, device.ssh_username or "admin", ssh_password
        )
    except Exception:
        pass

    priority = ChangePriority.EMERGENCY if drift.severity == DriftSeverity.CRITICAL else ChangePriority.HIGH

    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=actor.id,
        approved_by=actor.id,  # auto-remediation: requester is the approver, recorded explicitly
        priority=priority,
        description=f"Auto-remediate drift on {device.hostname}: push {baseline_label} to fix drift",
        business_justification=(
            f"Configuration drift auto-remediation for drift {drift.id} "
            f"(severity={drift.severity.value}, compliance={drift.compliance_score}/100). "
            f"Restoring {baseline_label}."
        ),
        current_config=current_config,
        proposed_config=baseline_config,
        status=ChangeStatus.APPROVED,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    drift.status = DriftStatus.ROLLED_BACK
    db.commit()

    audit_service.record_event(
        db, actor=actor.email, action="Drift Auto-Remediation Queued", result="Approved",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=f"drift_id={drift.id} baseline={baseline_label} severity={drift.severity.value}",
    )
    event_bus.publish_event(
        "change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id)
    )
    event_bus.publish_event("drift_detected", device_id=str(device.id), drift_id=str(drift.id), severity=drift.severity.value, compliance_score=drift.compliance_score)

    return cr


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


_SEVERITY_RANK = {
    DriftSeverity.CRITICAL: 3,
    DriftSeverity.HIGH: 2,
    DriftSeverity.MEDIUM: 1,
    DriftSeverity.LOW: 0,
}

# Matches a changed diff line (with the leading +/- already stripped) that
# is purely a cosmetic label edit -- an interface/ACL description or a
# remark/comment -- as opposed to anything that changes device behavior
# (an ACL entry, an interface's shutdown state, a routing statement, etc).
# Covers both CLI-style ("description WAN uplink", "! updated 2026-08",
# "remark allow-vpn") and structural-diff-derived cli_diff lines, which use
# the same vocabulary (see config_format_service.to_cli_commands).
_COSMETIC_LINE_RE = re.compile(r"^(no\s+)?(description|remark)\b|^!", re.IGNORECASE)


def _is_cosmetic_only_diff(diff_text: str) -> bool:
    """True if every added/removed line in a unified diff is a cosmetic
    description/remark/comment edit and at least one such line exists.
    Used to gate one-click bulk approval -- see is_low_risk_bulk_approvable.
    """
    saw_change = False
    for line in diff_text.splitlines():
        if not line or line[0] not in "+-" or line.startswith("+++") or line.startswith("---"):
            continue
        content = line[1:].strip()
        if not content:
            continue
        saw_change = True
        if not _COSMETIC_LINE_RE.match(content):
            return False
    return saw_change


def is_low_risk_bulk_approvable(drift: ConfigDrift) -> bool:
    """Eligible for the "Bulk-approve low-risk drift" one-click action:
    still OPEN, LOW severity, and every changed line is a cosmetic
    description/remark edit -- nothing that touches actual device behavior.
    Deliberately conservative: a LOW-severity drift that also touches real
    config (even one non-cosmetic line) is excluded, so the bulk path can
    never rubber-stamp a behavior change alongside a label change.
    """
    return (
        drift.status == DriftStatus.OPEN
        and drift.severity == DriftSeverity.LOW
        and _is_cosmetic_only_diff(drift.diff_text)
    )


def list_low_risk_candidates(db: Session, limit: int = 200) -> list[ConfigDrift]:
    """Every currently-open, cosmetic-only LOW drift -- the preview list
    behind GET /drift/low-risk-candidates and the default target set for
    POST /drift/bulk-approve when no explicit drift_ids are given."""
    open_low = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.status == DriftStatus.OPEN, ConfigDrift.severity == DriftSeverity.LOW)
        .order_by(ConfigDrift.detected_at.desc())
        .limit(limit)
        .all()
    )
    return [d for d in open_low if _is_cosmetic_only_diff(d.diff_text)]


def bulk_approve_drift(db: Session, drift_ids: list[uuid.UUID] | None, actor: "User") -> dict:
    """Approve a batch of low-risk-cosmetic drift records in one action.
    With drift_ids=None, approves every current candidate from
    list_low_risk_candidates (fleet-wide "approve everything safe" case).
    With explicit drift_ids, only the ones that still pass
    is_low_risk_bulk_approvable are approved -- anything else (already
    reviewed, or not actually cosmetic-only) is reported back as skipped
    rather than silently approved.
    """
    if drift_ids is not None:
        candidates = db.query(ConfigDrift).filter(ConfigDrift.id.in_(drift_ids)).all()
    else:
        candidates = list_low_risk_candidates(db)

    approved: list[ConfigDrift] = []
    skipped_ids: list[uuid.UUID] = []
    for d in candidates:
        if is_low_risk_bulk_approvable(d):
            d.status = DriftStatus.APPROVED
            approved.append(d)
        else:
            skipped_ids.append(d.id)

    if approved:
        db.commit()
        audit_service.record_event(
            db,
            actor=actor.email,
            action="Drift Bulk-Approved (low-risk cosmetic)",
            result=f"{len(approved)} approved",
            detail="drift_ids=" + ",".join(str(d.id) for d in approved),
        )
        for d in approved:
            event_bus.publish_event(
                "drift_detected",
                device_id=str(d.device_id),
                drift_id=str(d.id),
                severity=d.severity.value,
                compliance_score=d.compliance_score,
            )

    return {
        "approved_count": len(approved),
        "approved_ids": [d.id for d in approved],
        "skipped_ids": skipped_ids,
    }


def weekly_golden_config_drift(db: Session, days: int = 7) -> list[ConfigDrift]:
    """One-click "who's drifted from golden config this week" list: one row
    per device (its single most recent GOLDEN_CONFIG-baseline drift record
    detected within the last `days` days), not the raw per-scan event feed
    list_drifts() returns -- a device scanned nightly for a week would
    otherwise show up to 7 times for one ongoing drift.

    Devices with no actual change (added_lines == removed_lines == 0 --
    i.e. a scan that ran and found nothing) are excluded; this is a list
    of devices that drifted, not devices that were merely checked.
    Sorted most-severe-first, then most-recently-detected first.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    rows = (
        db.query(ConfigDrift)
        .filter(
            ConfigDrift.baseline == DriftBaseline.GOLDEN_CONFIG,
            ConfigDrift.detected_at >= since,
        )
        .order_by(ConfigDrift.detected_at.desc())
        .all()
    )

    latest_by_device: dict = {}
    for row in rows:
        if not (row.added_lines or row.removed_lines):
            continue
        # Rows are already ordered newest-first, so the first time a
        # device_id is seen here is that device's most recent drift.
        latest_by_device.setdefault(row.device_id, row)

    return sorted(
        latest_by_device.values(),
        key=lambda d: (_SEVERITY_RANK[d.severity], d.detected_at),
        reverse=True,
    )


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


def drift_trend(db: Session, days: int = 90, bucket_days: int = 7) -> list[dict]:
    """Fleet-wide drift event volume bucketed over time (default: weekly
    buckets over the last ~13 weeks) -- "is drift getting more or less
    frequent overall" for a dashboard trend chart. Complements
    fleet_summary (a snapshot of *currently open* drift) with a time
    series of *detection events*, resolved or not.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    rows = (
        db.query(ConfigDrift.detected_at, ConfigDrift.severity, ConfigDrift.device_id)
        .filter(ConfigDrift.detected_at >= since)
        .all()
    )

    buckets: dict[datetime.date, dict] = {}
    bucket_start = since.date()
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    cursor = bucket_start
    while cursor <= now_date:
        buckets[cursor] = {"bucket_start": cursor, "total": 0, "critical": 0, "high": 0, "devices": set()}
        cursor += datetime.timedelta(days=bucket_days)

    bucket_keys = sorted(buckets.keys())

    def _bucket_for(dt: datetime.datetime) -> datetime.date:
        d = dt.date()
        # Find the latest bucket_start <= d (buckets are evenly spaced from
        # bucket_start, so this is a simple reverse scan over a short list).
        chosen = bucket_keys[0]
        for k in bucket_keys:
            if k <= d:
                chosen = k
            else:
                break
        return chosen

    for detected_at, severity, device_id in rows:
        key = _bucket_for(detected_at)
        b = buckets[key]
        b["total"] += 1
        if severity == DriftSeverity.CRITICAL:
            b["critical"] += 1
        elif severity == DriftSeverity.HIGH:
            b["high"] += 1
        b["devices"].add(device_id)

    return [
        {
            "bucket_start": buckets[k]["bucket_start"],
            "total": buckets[k]["total"],
            "critical": buckets[k]["critical"],
            "high": buckets[k]["high"],
            "distinct_devices": len(buckets[k]["devices"]),
        }
        for k in bucket_keys
    ]


def flapping_devices(db: Session, days: int = 30, min_events: int = 3) -> list[dict]:
    """Devices whose config keeps drifting -- `min_events`+ drift
    detections in the last `days` days -- i.e. "is this device drifting
    more often lately / is someone repeatedly hand-editing it (flapping
    config)" instead of a one-off change. Ranked by event count
    descending, then by how recently it last drifted.

    This is a simple frequency heuristic, not a statistical anomaly
    detector: a device that's *supposed* to change often (e.g. one with
    frequent legitimate maintenance) will also show up here. It's meant
    to prompt a look, not to auto-flag wrongdoing.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    rows = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.detected_at >= since)
        .order_by(ConfigDrift.detected_at.desc())
        .all()
    )

    by_device: dict = {}
    for row in rows:
        entry = by_device.setdefault(
            row.device_id,
            {"device_id": row.device_id, "event_count": 0, "last_detected_at": row.detected_at, "max_severity": row.severity},
        )
        entry["event_count"] += 1
        if row.detected_at > entry["last_detected_at"]:
            entry["last_detected_at"] = row.detected_at
        if _SEVERITY_RANK[row.severity] > _SEVERITY_RANK[entry["max_severity"]]:
            entry["max_severity"] = row.severity

    results = [e for e in by_device.values() if e["event_count"] >= min_events]
    results.sort(key=lambda e: (e["event_count"], e["last_detected_at"]), reverse=True)
    return results
