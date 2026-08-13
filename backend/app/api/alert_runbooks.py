"""Alert Runbooks CRUD API — attach a remediation doc/playbook to an
alert category (optionally scoped to a source), so alerts of that type
surface the link directly.

  GET     /alert-runbooks          — list all mappings
  POST    /alert-runbooks          — create a mapping
  PUT     /alert-runbooks/{id}     — update a mapping
  DELETE  /alert-runbooks/{id}     — delete a mapping
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert_runbook import AlertRunbook
from app.models.user import User
from app.schemas.alert_runbook import (
    AlertRunbookCreate,
    AlertRunbookRead,
    AlertRunbookUpdate,
)

router = APIRouter(prefix="/alert-runbooks", tags=["alert-runbooks"])


@router.get("", response_model=list[AlertRunbookRead])
def list_alert_runbooks(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(AlertRunbook).order_by(AlertRunbook.category).all()


@router.post("", response_model=AlertRunbookRead, status_code=201)
def create_alert_runbook(
    body: AlertRunbookCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mapping = AlertRunbook(
        category=body.category,
        source=body.source,
        title=body.title,
        url=body.url,
        notes=body.notes,
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
    for field, value in body.model_dump(exclude_unset=True).items():
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
