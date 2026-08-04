from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.compliance_baseline import ComplianceBaseline
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.compliance_baseline import (
    ComplianceBaselineRead,
    ComplianceBaselineSet,
    ComplianceBaselineSummary,
)
from app.services import audit_service, config_format_service, snapshot_service

router = APIRouter(prefix="/compliance-baselines", tags=["compliance-baselines"])

# Same authority level as Golden Config (app.api.config_management): setting
# a baseline other devices get judged against is a governance action, not a
# routine read.
BASELINE_WRITE_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _device_counts_by_role(db: Session) -> dict[str, int]:
    rows = (
        db.query(Device.device_role, func.count(Device.id))
        .filter(Device.device_role.isnot(None))
        .group_by(Device.device_role)
        .all()
    )
    return {role: count for role, count in rows}


def _to_read(baseline: ComplianceBaseline, device_count: int) -> ComplianceBaselineRead:
    config_text = snapshot_service.decrypt_config(baseline.config_encrypted)
    is_xml = config_format_service.looks_like_xml(config_text)
    return ComplianceBaselineRead(
        device_role=baseline.device_role,
        config=config_text,
        config_pretty=config_format_service.pretty_xml(config_text) if is_xml else None,
        is_xml=is_xml,
        checksum=baseline.checksum,
        description=baseline.description,
        set_by=baseline.set_by,
        device_count=device_count,
        created_at=baseline.created_at,
        updated_at=baseline.updated_at,
    )


@router.get("", response_model=list[ComplianceBaselineSummary])
def list_compliance_baselines(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """All role baselines currently set, for the management list view.
    Doesn't include the full config body (same reasoning as any other
    list endpoint next to a "get one" detail endpoint) -- use GET
    /compliance-baselines/{device_role} for that.
    """
    counts = _device_counts_by_role(db)
    baselines = db.query(ComplianceBaseline).order_by(ComplianceBaseline.device_role).all()
    return [
        ComplianceBaselineSummary(
            device_role=b.device_role,
            checksum=b.checksum,
            description=b.description,
            set_by=b.set_by,
            device_count=counts.get(b.device_role, 0),
            updated_at=b.updated_at,
        )
        for b in baselines
    ]


@router.get("/device-roles", response_model=list[str])
def list_device_roles_in_use(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Distinct Device.device_role values currently assigned to at least
    one device -- lets the baseline picker suggest roles that actually
    need a baseline, without hardcoding a fixed role list (orgs name
    these differently)."""
    rows = (
        db.query(Device.device_role)
        .filter(Device.device_role.isnot(None))
        .distinct()
        .order_by(Device.device_role)
        .all()
    )
    return [r[0] for r in rows]


@router.get("/{device_role}", response_model=ComplianceBaselineRead)
def get_compliance_baseline(device_role: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    baseline = db.query(ComplianceBaseline).filter(ComplianceBaseline.device_role == device_role).first()
    if not baseline:
        raise HTTPException(
            status_code=404,
            detail=f"No compliance baseline set for role '{device_role}' yet. PUT this endpoint to set one.",
        )
    counts = _device_counts_by_role(db)
    return _to_read(baseline, counts.get(device_role, 0))


@router.put("/{device_role}", response_model=ComplianceBaselineRead)
def set_compliance_baseline(
    device_role: str,
    payload: ComplianceBaselineSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(BASELINE_WRITE_ROLES),
):
    """Sets (or replaces) the shared baseline template for every device
    with this device_role. Upsert, same as Golden Config: one row per
    role, a current approved state rather than a history.
    """
    if not payload.config or not payload.config.strip():
        raise HTTPException(status_code=400, detail="Compliance baseline config cannot be empty")

    baseline = db.query(ComplianceBaseline).filter(ComplianceBaseline.device_role == device_role).first()
    if baseline is None:
        baseline = ComplianceBaseline(device_role=device_role)
        db.add(baseline)

    baseline.config_encrypted = snapshot_service.encrypt_config(payload.config)
    baseline.checksum = snapshot_service.compute_checksum(payload.config)
    baseline.description = payload.description
    baseline.set_by = current_user.email
    db.commit()
    db.refresh(baseline)

    audit_service.record_event(
        db, actor=current_user.email, action="Compliance Baseline Set", result="Success",
        detail=f"device_role={device_role} checksum={baseline.checksum}",
    )

    counts = _device_counts_by_role(db)
    return _to_read(baseline, counts.get(device_role, 0))


@router.delete("/{device_role}", status_code=204)
def delete_compliance_baseline(
    device_role: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(BASELINE_WRITE_ROLES),
):
    baseline = db.query(ComplianceBaseline).filter(ComplianceBaseline.device_role == device_role).first()
    if not baseline:
        raise HTTPException(status_code=404, detail=f"No compliance baseline set for role '{device_role}'")
    db.delete(baseline)
    db.commit()

    audit_service.record_event(
        db, actor=current_user.email, action="Compliance Baseline Deleted", result="Success",
        detail=f"device_role={device_role}",
    )