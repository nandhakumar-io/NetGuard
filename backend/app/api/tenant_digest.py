"""Tenant Digest Subscription CRUD API.

  GET     /tenant-digest-subscriptions            -- list (scoped to caller's tenant unless MSP staff)
  POST    /tenant-digest-subscriptions             -- create
  PUT     /tenant-digest-subscriptions/{id}        -- update
  DELETE  /tenant-digest-subscriptions/{id}        -- delete
  POST    /tenant-digest-subscriptions/{id}/send-now -- out-of-cycle send (testing/manual)

Same visibility model as app.api.escalation_policies: a scoped
(non-MSP) caller only ever sees/manages their own tenant's subscriptions;
MSP staff (tenant_id scope is None) can manage any tenant's. Unlike
EscalationPolicy there's no "global" subscription concept here (nothing
like tenant_id=NULL) -- a digest is inherently about one tenant's
activity, so every row requires a real tenant_id and a scoped caller may
only ever create one for their own.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope
from app.models.tenant_digest_subscription import (
    DigestCadence,
    DigestSeverityFloor,
    TenantDigestSubscription,
)
from app.models.user import User
from app.schemas.tenant_digest import (
    TenantDigestSubscriptionCreate,
    TenantDigestSubscriptionRead,
    TenantDigestSubscriptionUpdate,
)

router = APIRouter(prefix="/tenant-digest-subscriptions", tags=["tenant-digest-subscriptions"])


def _get_scoped_subscription(db: Session, sub_id: uuid.UUID, tenant_id) -> TenantDigestSubscription:
    sub = db.get(TenantDigestSubscription, sub_id)
    if not sub or (tenant_id is not None and sub.tenant_id != tenant_id):
        raise HTTPException(status_code=404, detail="Digest subscription not found")
    return sub


@router.get("", response_model=list[TenantDigestSubscriptionRead])
def list_subscriptions(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    q = db.query(TenantDigestSubscription)
    if tenant_id is not None:
        q = q.filter(TenantDigestSubscription.tenant_id == tenant_id)
    return q.order_by(TenantDigestSubscription.created_at.desc()).all()


@router.post("", response_model=TenantDigestSubscriptionRead)
def create_subscription(
    payload: TenantDigestSubscriptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    # Scoped (non-MSP) caller: default to their own tenant when omitted,
    # and reject any attempt to name a different one. MSP staff (scope is
    # None) must name the target tenant explicitly -- there's no "own
    # tenant" to default to.
    target_tenant_id = payload.tenant_id
    if tenant_id is not None:
        if target_tenant_id is not None and target_tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot create a digest subscription for another tenant")
        target_tenant_id = tenant_id
    elif target_tenant_id is None:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    try:
        cadence = DigestCadence(payload.cadence)
        severity_floor = DigestSeverityFloor(payload.severity_floor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if cadence == DigestCadence.WEEKLY and payload.day_of_week is None:
        raise HTTPException(status_code=422, detail="day_of_week is required for weekly cadence")
    if not (0 <= payload.hour_utc <= 23):
        raise HTTPException(status_code=422, detail="hour_utc must be between 0 and 23")

    sub = TenantDigestSubscription(
        tenant_id=target_tenant_id,
        cadence=cadence,
        hour_utc=payload.hour_utc,
        day_of_week=payload.day_of_week if cadence == DigestCadence.WEEKLY else None,
        recipients=payload.recipients,
        severity_floor=severity_floor,
        is_active=payload.is_active,
        created_by=user.email,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


@router.put("/{sub_id}", response_model=TenantDigestSubscriptionRead)
def update_subscription(
    sub_id: uuid.UUID,
    payload: TenantDigestSubscriptionUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    sub = _get_scoped_subscription(db, sub_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)

    if "cadence" in data:
        try:
            sub.cadence = DigestCadence(data["cadence"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "severity_floor" in data:
        try:
            sub.severity_floor = DigestSeverityFloor(data["severity_floor"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "hour_utc" in data:
        if not (0 <= data["hour_utc"] <= 23):
            raise HTTPException(status_code=422, detail="hour_utc must be between 0 and 23")
        sub.hour_utc = data["hour_utc"]
    if "day_of_week" in data:
        sub.day_of_week = data["day_of_week"]
    if "recipients" in data:
        sub.recipients = data["recipients"]
    if "is_active" in data:
        sub.is_active = data["is_active"]

    if sub.cadence == DigestCadence.WEEKLY and sub.day_of_week is None:
        raise HTTPException(status_code=422, detail="day_of_week is required for weekly cadence")
    if sub.cadence == DigestCadence.DAILY:
        sub.day_of_week = None

    db.commit()
    db.refresh(sub)
    return sub


@router.delete("/{sub_id}")
def delete_subscription(
    sub_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    sub = _get_scoped_subscription(db, sub_id, tenant_id)
    db.delete(sub)
    db.commit()
    return {"status": "deleted"}


@router.post("/{sub_id}/send-now", response_model=TenantDigestSubscriptionRead)
def send_now(
    sub_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Out-of-cycle send, for testing a subscription's recipients/content
    without waiting for its next scheduled hour. Advances last_sent_at
    same as a normal scheduled delivery, so this also has the effect of
    resetting the digest window -- same trade-off as any other manual
    "run now" action in this codebase (e.g.
    escalation_policies.run_now).
    """
    from app.services import tenant_digest_service

    sub = _get_scoped_subscription(db, sub_id, tenant_id)
    tenant_digest_service.deliver_due_subscription(db, sub)
    db.refresh(sub)
    return sub
