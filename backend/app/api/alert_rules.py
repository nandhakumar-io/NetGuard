"""Alert Rules CRUD API — user-configurable threshold-based alert conditions.

  GET     /alert-rules          — list all rules
  POST    /alert-rules          — create a new rule
  PUT     /alert-rules/{id}     — update a rule
  DELETE  /alert-rules/{id}     — delete a rule
  PATCH   /alert-rules/{id}/toggle — enable/disable a rule

Tenant-scoped (migration 0095): a rule created by a regular customer-side
user only applies to -- and is only visible/editable by -- that user's
tenant. An MSP-staff-created rule (tenant_id NULL) applies across every
tenant and is visible to everyone, same "global unless scoped" convention
as api.webhooks. See app.core.deps.get_tenant_scope.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope
from app.models.alert_rule import AlertRule
from app.models.user import User
from app.schemas.alert_rule import AlertRuleCreate, AlertRuleRead, AlertRuleUpdate

router = APIRouter(prefix="/alert-rules", tags=["alert-rules"])


def _get_scoped_rule(db: Session, rule_id: uuid.UUID, tenant_id) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if not rule or (tenant_id is not None and rule.tenant_id not in (None, tenant_id)):
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


@router.get("", response_model=list[AlertRuleRead])
def list_alert_rules(
    enabled_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    q = db.query(AlertRule)
    if tenant_id is not None:
        q = q.filter((AlertRule.tenant_id == tenant_id) | (AlertRule.tenant_id.is_(None)))
    if enabled_only:
        q = q.filter(AlertRule.enabled == True)
    return q.order_by(AlertRule.created_at.desc()).limit(limit).all()


@router.post("", response_model=AlertRuleRead, status_code=201)
def create_alert_rule(
    body: AlertRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    rule = AlertRule(
        name=body.name,
        description=body.description,
        metric=body.metric,
        operator=body.operator,
        threshold=body.threshold,
        severity=body.severity,
        scope_vendor=body.scope_vendor,
        scope_site=body.scope_site,
        scope_device_role=body.scope_device_role,
        cooldown_seconds=body.cooldown_seconds,
        enabled=body.enabled,
        created_by=user.email,
        # None only for an MSP-staff creator -- see get_tenant_scope.
        tenant_id=tenant_id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/{rule_id}", response_model=AlertRuleRead)
def update_alert_rule(
    rule_id: uuid.UUID,
    body: AlertRuleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
def delete_alert_rule(
    rule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    db.delete(rule)
    db.commit()


@router.patch("/{rule_id}/toggle", response_model=AlertRuleRead)
def toggle_alert_rule(
    rule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    rule.enabled = not rule.enabled
    db.commit()
    db.refresh(rule)
    return rule
