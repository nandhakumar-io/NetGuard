import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope, require_roles
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.drift import (
    BulkApproveRequest,
    BulkApproveResponse,
    ComplianceRollupResponse,
    DriftDetail,
    DriftFleetSummary,
    DriftRead,
    DriftScanRequest,
    DriftScanResponse,
    DriftStatusUpdate,
    DriftTrendResponse,
    FlappingDeviceEntry,
    FlappingDevicesResponse,
    LowRiskDriftCandidate,
    RollbackRecommendationResponse,
    TenantComplianceRollup,
    WeeklyGoldenDriftEntry,
    WeeklyGoldenDriftGroup,
    WeeklyGoldenDriftReport,
)
from app.services import audit_service, drift_service

router = APIRouter(tags=["drift"])

# Reviewing/dismissing/approving a drift changes its record of what's
# "acceptable" on the device, same authority level as approving a change.
DRIFT_REVIEW_ROLES = require_roles(UserRole.NETWORK_ADMIN)


@router.get("/drift/summary", response_model=DriftFleetSummary)
def get_fleet_drift_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Powers the Drift Dashboard Widget (fleet-wide drift posture)."""
    return drift_service.fleet_summary(db)


@router.get("/drift/compliance-rollup", response_model=ComplianceRollupResponse)
def get_compliance_rollup(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Per-tenant compliance dashboard rollup: what fraction of each
    tenant's fleet is currently free of open drift ("in baseline"), for
    the same "how healthy is each customer" glance the tenant notification
    digest gives for alert volume. A scoped (non-MSP) caller gets a
    single-tenant array (their own); MSP staff (tenant_id is None) get
    every active tenant, worst-compliance first -- same ordering
    convention as app.api.tenant_board.get_tenant_board.

    "In baseline" here means zero currently-OPEN ConfigDrift rows for
    that device -- a binary presence/absence measure, distinct from
    ConfigDrift.compliance_score (0-100 severity of a *given* drift) that
    drift_service.fleet_summary already averages fleet-wide. This is the
    per-tenant complement to that fleet-wide number.
    """
    tenants_q = db.query(Tenant).filter(Tenant.is_active.is_(True))
    if tenant_id is not None:
        tenants_q = tenants_q.filter(Tenant.id == tenant_id)
    tenants = tenants_q.order_by(Tenant.name).all()
    if not tenants:
        return ComplianceRollupResponse(tenants=[])

    tenant_ids = [t.id for t in tenants]

    device_counts = dict(
        db.query(Device.tenant_id, func.count(Device.id))
        .filter(Device.tenant_id.in_(tenant_ids))
        .group_by(Device.tenant_id)
        .all()
    )

    # Devices with at least one OPEN drift, and the count/avg score of
    # that open drift, grouped by tenant via the drift's device.
    drift_rows = (
        db.query(Device.tenant_id, ConfigDrift.device_id, ConfigDrift.compliance_score)
        .join(Device, Device.id == ConfigDrift.device_id)
        .filter(ConfigDrift.status == DriftStatus.OPEN, Device.tenant_id.in_(tenant_ids))
        .all()
    )
    devices_out_by_tenant: dict[uuid.UUID, set[uuid.UUID]] = {}
    scores_by_tenant: dict[uuid.UUID, list[int]] = {}
    for t_id, device_id, score in drift_rows:
        devices_out_by_tenant.setdefault(t_id, set()).add(device_id)
        scores_by_tenant.setdefault(t_id, []).append(score)

    rows: list[TenantComplianceRollup] = []
    for tenant in tenants:
        total = device_counts.get(tenant.id, 0)
        out_of_baseline = len(devices_out_by_tenant.get(tenant.id, ()))
        in_baseline = total - out_of_baseline
        pct = round((in_baseline / total) * 100, 1) if total else 100.0
        scores = scores_by_tenant.get(tenant.id, [])
        avg_score = round(sum(scores) / len(scores), 1) if scores else None

        rows.append(
            TenantComplianceRollup(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                tenant_slug=tenant.slug,
                device_count=total,
                devices_in_baseline=in_baseline,
                devices_out_of_baseline=out_of_baseline,
                compliance_pct=pct,
                open_drift_count=len(scores),
                average_open_drift_score=avg_score,
            )
        )

    # Worst-compliance tenants first -- same "the point of a rollup is to
    # surface who needs attention" ordering as the tenant board.
    rows.sort(key=lambda r: r.compliance_pct)
    return ComplianceRollupResponse(tenants=rows)


@router.get("/drift/trends", response_model=DriftTrendResponse)
def get_drift_trends(
    days: int = 90,
    bucket_days: int = 7,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide drift event volume bucketed over time — powers the
    Drift Trend bar-chart on the Drift page. A rising trend means
    devices are drifting more often, not just that more scans ran."""
    points = drift_service.drift_trend(db, days=days, bucket_days=bucket_days)
    return DriftTrendResponse(days=days, bucket_days=bucket_days, points=points)


@router.get("/drift/flapping", response_model=FlappingDevicesResponse)
def get_flapping_devices(
    days: int = 30,
    min_events: int = 3,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Devices whose config keeps drifting — a sign of repeated
    hand-edits. Powers the Flapping Devices panel on the Drift page."""
    raw = drift_service.flapping_devices(db, days=days, min_events=min_events)
    device_ids = [e["device_id"] for e in raw]
    hostnames = {
        d.id: d.hostname
        for d in db.query(Device).filter(Device.id.in_(device_ids)).all()
    } if device_ids else {}
    entries = [
        FlappingDeviceEntry(
            device_id=e["device_id"],
            hostname=hostnames.get(e["device_id"], str(e["device_id"])),
            event_count=e["event_count"],
            last_detected_at=e["last_detected_at"],
            max_severity=e["max_severity"],
        )
        for e in raw
    ]
    return FlappingDevicesResponse(days=days, min_events=min_events, devices=entries)


@router.get("/drift", response_model=list[DriftRead])
def list_all_drifts(
    status: DriftStatus | None = None,
    severity: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide drift feed for the Drift page, newest first."""
    from app.models.config_drift import DriftSeverity

    sev = DriftSeverity(severity) if severity else None
    return drift_service.list_drifts(db, status=status, severity=sev)


@router.get("/drift/report/weekly-golden-config", response_model=WeeklyGoldenDriftReport)
def weekly_golden_config_drift_report(
    days: int = 7,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """One-click fleet view: every device that has drifted from its golden
    config in the last `days` days (default 7 -- "this week"), one row per
    device rather than the raw per-scan drift feed. Complements GET /drift
    (which is the full per-event feed, filterable by device/severity but
    not deduplicated per device or scoped to a time window).

    Also bucketed by DeviceGroup (`groups`) -- a NOC digest read per-team
    or per-fleet ("who on Edge Firewalls drifted this week") rather than
    one long undifferentiated table. Devices with no DeviceGroup assigned
    land in a synthetic "Ungrouped" bucket (group_id=None).
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    drifts = drift_service.weekly_golden_config_drift(db, days=days)

    device_ids = {d.device_id for d in drifts}
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all() if device_ids else []
    devices_by_id = {d.id: d for d in devices}

    group_ids = {d.group_id for d in devices if d.group_id is not None}
    group_names = {g.id: g.name for g in db.query(DeviceGroup).filter(DeviceGroup.id.in_(group_ids)).all()} if group_ids else {}

    entries = [
        WeeklyGoldenDriftEntry(
            **DriftRead.model_validate(d).model_dump(),
            hostname=devices_by_id[d.device_id].hostname if d.device_id in devices_by_id else str(d.device_id),
        )
        for d in drifts
    ]

    entries_by_group: dict[uuid.UUID | None, list[WeeklyGoldenDriftEntry]] = {}
    for entry, drift_row in zip(entries, drifts):
        device = devices_by_id.get(drift_row.device_id)
        group_key = device.group_id if device else None
        entries_by_group.setdefault(group_key, []).append(entry)

    # Named groups first (alphabetical), "Ungrouped" last -- matches how
    # the Groups page orders things elsewhere in the app.
    ordered_group_ids = sorted(
        (gid for gid in entries_by_group if gid is not None),
        key=lambda gid: group_names.get(gid, ""),
    )
    groups = [
        WeeklyGoldenDriftGroup(
            group_id=gid,
            group_name=group_names.get(gid, "Unnamed group"),
            devices=entries_by_group[gid],
        )
        for gid in ordered_group_ids
    ]
    if None in entries_by_group:
        groups.append(
            WeeklyGoldenDriftGroup(group_id=None, group_name="Ungrouped", devices=entries_by_group[None])
        )

    return WeeklyGoldenDriftReport(since=since, days=days, devices=entries, groups=groups)


@router.get("/drift/low-risk-candidates", response_model=list[LowRiskDriftCandidate])
def get_low_risk_drift_candidates(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Preview list behind "Bulk-approve low-risk drift": every OPEN,
    LOW-severity drift where every changed line is a cosmetic
    description/remark edit -- e.g. someone updated an interface
    description, nothing that changes device behavior.
    """
    candidates = drift_service.list_low_risk_candidates(db)
    device_ids = {d.device_id for d in candidates}
    hostnames = {d.id: d.hostname for d in db.query(Device).filter(Device.id.in_(device_ids)).all()} if device_ids else {}
    return [
        LowRiskDriftCandidate(
            **DriftRead.model_validate(d).model_dump(),
            hostname=hostnames.get(d.device_id, str(d.device_id)),
        )
        for d in candidates
    ]


@router.post("/drift/bulk-approve", response_model=BulkApproveResponse)
def bulk_approve_drift(
    payload: BulkApproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(DRIFT_REVIEW_ROLES),
):
    """Approve a batch of low-risk-cosmetic drift in one action, instead of
    reviewing each description/remark-only drift one at a time. Only ever
    touches drift that independently qualifies as low-risk-cosmetic (see
    drift_service.is_low_risk_bulk_approvable) -- passing drift_ids doesn't
    bypass that check, it just narrows the candidate set.
    """
    return drift_service.bulk_approve_drift(db, payload.drift_ids, current_user)


@router.get("/drift/{drift_id}", response_model=DriftDetail)
def get_drift(drift_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")
    return drift


@router.get("/drift/{drift_id}/rollback-recommendation", response_model=RollbackRecommendationResponse)
def get_rollback_recommendation(drift_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")
    return drift_service.rollback_recommendation(drift)


@router.post("/drift/{drift_id}/remediate", response_model=dict, status_code=202)
def remediate_drift(
    drift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(DRIFT_REVIEW_ROLES),
):
    """One-click "push golden config to fix drift" -- submits a standard
    change request that restores this drift's baseline (golden config or
    role baseline) to the device, into the normal PENDING_APPROVAL review
    queue. This only submits the change; it still needs a NETWORK_ADMIN
    approval (a second, different one if it's Critical Risk) via the usual
    change-request approve endpoint before anything deploys -- previously
    drift was detect-only, this is the auto-fill/submit counterpart to
    GET .../drift/{id}/rollback-recommendation, which only advises.
    """
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")

    device = db.get(Device, drift.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device for this drift record no longer exists")

    try:
        cr = drift_service.remediate_drift(db, drift, device, current_user)
    except drift_service.RemediationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {
        "message": (
            f"Remediation change request submitted for {device.hostname} and awaiting approval"
            + (" (dual approval required)." if cr.requires_dual_approval else ".")
        ),
        "change_request_id": str(cr.id),
        "device_id": str(device.id),
        "requires_dual_approval": cr.requires_dual_approval,
    }


@router.patch("/drift/{drift_id}", response_model=DriftRead)
def update_drift_status(
    drift_id: uuid.UUID,
    payload: DriftStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(DRIFT_REVIEW_ROLES),
):
    """Mark a drift as approved (accepted as the new normal), dismissed
    (false positive / expected), or manually reconciled -- does not touch
    the device itself. To actually fix the device, use
    POST /devices/{id}/rollback with a snapshot, same as any other
    rollback; that flow can set this record's status to ROLLED_BACK
    separately once it succeeds.
    """
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")

    device = db.get(Device, drift.device_id)
    drift.status = payload.status
    db.commit()
    db.refresh(drift)

    audit_service.record_event(
        db,
        actor=current_user.email, tenant_id=current_user.tenant_id,
        action="Drift Reviewed",
        result=payload.status.value,
        device_hostname=device.hostname if device else None,
        detail=f"drift_id={drift.id}",
    )
    return drift


@router.get("/devices/{device_id}/drift", response_model=list[DriftRead])
def list_device_drift_history(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return drift_service.list_drifts(db, device_id=device_id)


@router.post("/devices/{device_id}/drift/scan", response_model=DriftScanResponse)
def scan_device_drift(
    device_id: uuid.UUID,
    payload: DriftScanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """On-demand drift scan (SRS: nightly automated + on-demand). Reads the
    device's live running config, compares it against the requested
    baseline, and persists a new ConfigDrift record.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        result = drift_service.detect_drift(db, device, baseline=payload.baseline, triggered_by=current_user.email)
    except drift_service.NoBaselineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return DriftScanResponse(
        drift=result.drift,
        baseline_label=result.baseline_label,
        findings=result.findings,
        rollback_recommendation=drift_service.rollback_recommendation(result.drift),
    )
