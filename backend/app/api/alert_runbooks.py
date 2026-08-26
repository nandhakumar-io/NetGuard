"""Alert Runbooks CRUD API — attach a remediation doc/playbook to an
alert category (optionally scoped to a source), so alerts of that type
surface the link directly. Optionally, a runbook can also carry an
actual remediation action (restart a service / push a config snippet)
that can be triggered for real against a device -- see
POST /alert-runbooks/{id}/execute and
app.services.runbook_execution_service.

  GET     /alert-runbooks                    — list all mappings
  POST    /alert-runbooks                    — create a mapping
  PUT     /alert-runbooks/{id}                — update a mapping
  DELETE  /alert-runbooks/{id}                — delete a mapping
  POST    /alert-runbooks/{id}/execute        — run the remediation action
  GET     /alert-runbooks/{id}/executions     — remediation run history
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.alert_runbook import AlertRunbook, RemediationActionType
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.alert_runbook import (
    AlertRunbookCreate,
    AlertRunbookRead,
    AlertRunbookUpdate,
    RunbookExecutionRead,
    RunbookExecutionRequest,
)
from app.services import runbook_execution_service

router = APIRouter(prefix="/alert-runbooks", tags=["alert-runbooks"])


def _coerce_remediation_fields(data: dict) -> dict:
    """Converts the plain strings PATCH/POST bodies carry for
    remediation_action_type / remediation_required_role into the actual
    Python enum members SQLAlchemy's Enum columns expect -- same
    string-to-enum conversion push_subscriptions.py does for `provider`,
    just inline here since there are two enum fields instead of one.
    """
    if "remediation_action_type" in data and data["remediation_action_type"] is not None:
        try:
            data["remediation_action_type"] = RemediationActionType(data["remediation_action_type"])
        except ValueError:
            raise HTTPException(status_code=400, detail="remediation_action_type must be 'restart_service' or 'push_config'")
    if "remediation_required_role" in data and data["remediation_required_role"] is not None:
        try:
            data["remediation_required_role"] = UserRole(data["remediation_required_role"])
        except ValueError:
            raise HTTPException(status_code=400, detail="remediation_required_role must be a valid user role")
    if data.get("remediation_enabled") and not data.get("remediation_command"):
        raise HTTPException(status_code=400, detail="remediation_command is required when remediation_enabled is true")
    return data


@router.get("", response_model=list[AlertRunbookRead])
def list_alert_runbooks(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(AlertRunbook).order_by(AlertRunbook.category).all()


@router.post("", response_model=AlertRunbookRead, status_code=201)
def create_alert_runbook(
    body: AlertRunbookCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fields = _coerce_remediation_fields(body.model_dump())
    mapping = AlertRunbook(
        category=fields["category"],
        source=fields["source"],
        title=fields["title"],
        url=fields["url"],
        notes=fields["notes"],
        remediation_enabled=fields["remediation_enabled"],
        remediation_action_type=fields["remediation_action_type"],
        remediation_label=fields["remediation_label"],
        remediation_command=fields["remediation_command"],
        remediation_required_role=fields["remediation_required_role"],
        created_by=user.email,
    )
    db.add(mapping)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A runbook is already mapped for this category/source combination",
        )
    db.refresh(mapping)
    return mapping


@router.put("/{runbook_id}", response_model=AlertRunbookRead)
def update_alert_runbook(
    runbook_id: uuid.UUID,
    body: AlertRunbookUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    mapping = db.get(AlertRunbook, runbook_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Alert runbook not found")
    updates = _coerce_remediation_fields(body.model_dump(exclude_unset=True))
    for field, value in updates.items():
        setattr(mapping, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A runbook is already mapped for this category/source combination",
        )
    db.refresh(mapping)
    return mapping


@router.delete("/{runbook_id}", status_code=204)
def delete_alert_runbook(
    runbook_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    mapping = db.get(AlertRunbook, runbook_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Alert runbook not found")
    db.delete(mapping)
    db.commit()


@router.post("/{runbook_id}/execute", response_model=RunbookExecutionRead)
def execute_runbook_remediation(
    runbook_id: uuid.UUID,
    body: RunbookExecutionRequest,
    db: Session = Depends(get_db),
    # Same gate as every other device-write endpoint in the app (config
    # deploy, rollback, firmware upgrade): base NETWORK_ADMIN, an
    # extra_roles/extra_permissions grant that implies it, or an active
    # JIT elevation to network_admin. This is the RBAC/JIT gating layer;
    # runbook_execution_service adds an optional second, stricter role
    # check on top via AlertRunbook.remediation_required_role.
    user: User = Depends(require_roles(UserRole.NETWORK_ADMIN)),
):
    mapping = db.get(AlertRunbook, runbook_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Alert runbook not found")

    device = db.get(Device, body.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        execution = runbook_execution_service.trigger_remediation(
            db, runbook=mapping, device=device, user=user, alert_id=body.alert_id,
        )
    except runbook_execution_service.RemediationNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except runbook_execution_service.RemediationForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return execution


@router.get("/{runbook_id}/executions", response_model=list[RunbookExecutionRead])
def list_runbook_executions(
    runbook_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not db.get(AlertRunbook, runbook_id):
        raise HTTPException(status_code=404, detail="Alert runbook not found")
    return runbook_execution_service.list_executions_for_runbook(db, runbook_id)
