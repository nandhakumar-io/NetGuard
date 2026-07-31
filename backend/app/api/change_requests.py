import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.schemas.change_request import ChangeRequestCreate, ChangeRequestRead, RiskAnalysisResult
from app.services import diff_engine, risk_engine, validation_engine, audit_service

router = APIRouter(prefix="/change-requests", tags=["change-requests"])

# TODO: replace with real authenticated user once auth middleware/dependency is wired in
SYSTEM_ACTOR = "system"


@router.get("", response_model=list[ChangeRequestRead])
def list_change_requests(db: Session = Depends(get_db)):
    return db.query(ChangeRequest).order_by(ChangeRequest.created_at.desc()).all()


@router.post("", response_model=ChangeRequestRead, status_code=201)
def create_change_request(payload: ChangeRequestCreate, db: Session = Depends(get_db)):
    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # NOTE: current_config would normally be fetched live from the device.
    # For the prototype we leave it empty unless supplied by the caller later.
    current_config = None

    diff_text = diff_engine.generate_diff(current_config, payload.proposed_config)
    validation = validation_engine.validate_syntax(payload.proposed_config)
    risk: RiskAnalysisResult = risk_engine.analyze(payload.proposed_config, current_config)

    cr = ChangeRequest(
        device_id=payload.device_id,
        submitted_by=uuid.uuid4(),  # placeholder until auth dependency provides the real user id
        priority=payload.priority,
        description=payload.description,
        business_justification=payload.business_justification,
        maintenance_window_start=payload.maintenance_window_start,
        maintenance_window_end=payload.maintenance_window_end,
        current_config=current_config,
        proposed_config=payload.proposed_config,
        config_diff=diff_text,
        risk_score=risk.risk_score,
        risk_findings="; ".join(risk.findings),
        status=ChangeStatus.PENDING_APPROVAL if validation.passed else ChangeStatus.DRAFT,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    audit_service.record_event(
        db,
        actor=SYSTEM_ACTOR,
        action="Submitted CR",
        result="Success" if validation.passed else "Validation Failed",
        device_hostname=device.hostname,
        change_request_id=cr.id,
        detail="; ".join(validation.errors) if validation.errors else None,
    )

    return cr


@router.get("/{cr_id}", response_model=ChangeRequestRead)
def get_change_request(cr_id: uuid.UUID, db: Session = Depends(get_db)):
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    return cr


@router.post("/{cr_id}/approve", response_model=ChangeRequestRead)
def approve_change_request(cr_id: uuid.UUID, db: Session = Depends(get_db)):
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.status != ChangeStatus.PENDING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"Cannot approve a request in status '{cr.status.value}'")

    cr.status = ChangeStatus.APPROVED
    db.commit()
    db.refresh(cr)

    device = db.get(Device, cr.device_id)
    audit_service.record_event(
        db, actor=SYSTEM_ACTOR, action="Approved", result="Approved",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
    )
    return cr


@router.post("/{cr_id}/reject", response_model=ChangeRequestRead)
def reject_change_request(cr_id: uuid.UUID, db: Session = Depends(get_db)):
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    cr.status = ChangeStatus.REJECTED
    db.commit()
    db.refresh(cr)

    device = db.get(Device, cr.device_id)
    audit_service.record_event(
        db, actor=SYSTEM_ACTOR, action="Rejected", result="Rejected",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
    )
    return cr
