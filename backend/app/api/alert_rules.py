"""Alert Rules CRUD API — user-configurable threshold-based alert conditions.

  GET     /alert-rules          — list all rules
  POST    /alert-rules          — create a new rule
  PUT     /alert-rules/{id}     — update a rule
  DELETE  /alert-rules/{id}     — delete a rule
  PATCH   /alert-rules/{id}/toggle — enable/disable a rule
  POST    /alert-rules/dry-run       — backtest an ad-hoc (not-yet-saved) rule
  POST    /alert-rules/{id}/dry-run  — backtest an existing rule's saved config

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
from app.schemas.alert_rule_backtest import (
    AlertRuleDryRunRequest,
    AlertRuleDryRunResponse,
)
from app.services import alert_rule_backtest_service

router = APIRouter(prefix="/alert-rules", tags=["alert-rules"])


def _get_scoped_rule(db: Session, rule_id: uuid.UUID, tenant_id) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if not rule or (tenant_id is not None and rule.tenant_id not in (None, tenant_id)):
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


def _dry_run_response(result: alert_rule_backtest_service.BacktestResult) -> AlertRuleDryRunResponse:
    return AlertRuleDryRunResponse(
        supported=result.supported,
        unsupported_reason=result.unsupported_reason,
        metric=result.metric,
        operator=result.operator,
        threshold=result.threshold,
        cooldown_seconds=result.cooldown_seconds,
        window_start=result.window_start,
        window_end=result.window_end,
        devices_matched=result.devices_matched,
        devices_with_data=result.devices_with_data,
        total_firings=result.total_firings,
        total_suppressed_by_cooldown=result.total_suppressed_by_cooldown,
        estimated_alerts_per_day=result.estimated_alerts_per_day,
        firings=[
            {
                "device_id": f.device_id, "hostname": f.hostname, "fired_at": f.fired_at,
                "cleared_at": f.cleared_at, "duration_seconds": f.duration_seconds, "peak_value": f.peak_value,
            }
            for f in result.firings
        ],
        per_device=[
            {
                "device_id": d.device_id, "hostname": d.hostname, "firing_count": d.firing_count,
                "suppressed_by_cooldown_count": d.suppressed_by_cooldown_count,
                "total_seconds_breached": d.total_seconds_breached, "samples_evaluated": d.samples_evaluated,
            }
            for d in result.per_device
        ],
    )


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


@router.post("/dry-run", response_model=AlertRuleDryRunResponse)
def dry_run_alert_rule(
    body: AlertRuleDryRunRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """\"Would this rule have fired against real metric history?\" preview
    for a rule that's still being drafted -- doesn't require (or create)
    a saved AlertRule. Answers the question a threshold set blind and
    only tuned reactively after a 3am page never gets asked first: how
    often would this actually have gone off over the last N hours of
    real fleet data, and against how many devices. See
    app.services.alert_rule_backtest_service for what's and isn't
    replayable, and why.
    """
    result = alert_rule_backtest_service.backtest_rule(
        db,
        tenant_id=tenant_id,
        metric=body.metric,
        operator=body.operator,
        threshold=body.threshold,
        cooldown_seconds=body.cooldown_seconds,
        scope_vendor=body.scope_vendor,
        scope_site=body.scope_site,
        scope_device_role=body.scope_device_role,
        lookback_hours=body.lookback_hours,
    )
    return _dry_run_response(result)


@router.post("/{rule_id}/dry-run", response_model=AlertRuleDryRunResponse)
def dry_run_existing_alert_rule(
    rule_id: uuid.UUID,
    lookback_hours: int = Query(168, ge=1, le=24 * 90),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Same preview as POST /alert-rules/dry-run, but replays an
    *already-saved* rule's exact configuration -- e.g. to sanity-check
    an existing rule that's paging too often, or to check whether it's
    safe to tighten/loosen before editing it.
    """
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    result = alert_rule_backtest_service.backtest_rule(
        db,
        tenant_id=tenant_id,
        metric=rule.metric.value if hasattr(rule.metric, "value") else rule.metric,
        operator=rule.operator.value if hasattr(rule.operator, "value") else rule.operator,
        threshold=rule.threshold,
        cooldown_seconds=rule.cooldown_seconds,
        scope_vendor=rule.scope_vendor,
        scope_site=rule.scope_site,
        scope_device_role=rule.scope_device_role,
        lookback_hours=lookback_hours,
    )
    return _dry_run_response(result)
