"""Escalation Policies CRUD API + escalation log/feed.

  GET     /escalation-policies             — list all policies
  POST    /escalation-policies             — create a new policy
  PUT     /escalation-policies/{id}        — update a policy
  DELETE  /escalation-policies/{id}        — delete a policy
  PATCH   /escalation-policies/{id}/toggle — enable/disable a policy
  GET     /escalation-policies/log         — feed of alerts that have been escalated
  GET     /escalation-policies/on-call-load — escalations per contact/policy over time
  POST    /escalation-policies/run-now     — trigger an out-of-cycle sweep (testing/manual)
"""
import datetime
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.escalation_policy import EscalationPolicy
from app.models.user import User
from app.schemas.escalation_policy import (
    EscalatedAlertRead,
    EscalationPolicyCreate,
    EscalationPolicyRead,
    EscalationPolicyUpdate,
)
from app.services import escalation_service

router = APIRouter(prefix="/escalation-policies", tags=["escalation-policies"])


@router.get("", response_model=list[EscalationPolicyRead])
def list_escalation_policies(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(EscalationPolicy)
    if enabled_only:
        q = q.filter(EscalationPolicy.enabled == True)
    return q.order_by(EscalationPolicy.created_at.desc()).all()


@router.get("/log", response_model=list[EscalatedAlertRead])
def escalation_log(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Feed of every alert that has been escalated at least once, most
    recently escalated first -- the "who got paged and when" view."""
    alerts = escalation_service.list_escalated_alerts(db, limit=limit)
    policy_ids = {a.escalation_policy_id for a in alerts if a.escalation_policy_id}
    names = {p.id: p.name for p in db.query(EscalationPolicy).filter(EscalationPolicy.id.in_(policy_ids)).all()} if policy_ids else {}

    results = []
    for a in alerts:
        obj = EscalatedAlertRead.model_validate(a)
        obj.escalation_policy_name = names.get(a.escalation_policy_id)
        results.append(obj)
    return results


@router.get("/on-call-load")
def on_call_load(
    days: int = Query(30, ge=1, le=365, description="Trailing window in days"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """On-call load: how many escalations each policy (and, within it,
    each secondary contact) has fired over the trailing window, plus a
    daily trend series -- "who's getting paged, how often, and is it
    trending up".

    Alert only stores the *first* escalation timestamp (`escalated_at`)
    and the most recent one (`last_escalated_at`) plus a running
    `escalation_count`, not a timestamped row per repeat firing (see
    app.models.escalation_policy.EscalationPolicy's docstring on
    repeat_minutes) -- so the daily trend below is bucketed by
    `escalated_at` (when the alert first breached the policy), with each
    alert's full `escalation_count` counted as load in that bucket. That
    undercounts *which* day repeat pages landed on for long-lived
    unacknowledged alerts, but correctly reflects total on-call load
    attributable to each policy/contact over the window.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    policies = {p.id: p for p in db.query(EscalationPolicy).all()}

    escalated_alerts = (
        db.query(Alert)
        .filter(Alert.escalated == True, Alert.escalated_at >= since)
        .order_by(Alert.escalated_at)
        .all()
    )

    by_policy: dict[uuid.UUID, dict] = {}
    by_contact: dict[str, dict] = defaultdict(lambda: {"escalations": 0, "alerts": 0})
    daily: dict[str, dict] = defaultdict(lambda: {"date": None, "escalations": 0, "alerts": 0})

    for alert in escalated_alerts:
        policy = policies.get(alert.escalation_policy_id)
        count = alert.escalation_count or 1
        day = alert.escalated_at.date().isoformat()
        daily[day]["date"] = day
        daily[day]["escalations"] += count
        daily[day]["alerts"] += 1

        if policy:
            entry = by_policy.setdefault(policy.id, {
                "policy_id": str(policy.id),
                "policy_name": policy.name,
                "channel": policy.channel.value if hasattr(policy.channel, "value") else policy.channel,
                "escalations": 0,
                "alerts": 0,
            })
            entry["escalations"] += count
            entry["alerts"] += 1

            contacts = [c.strip() for c in (policy.secondary_contacts or "").split(",") if c.strip()]
            if not contacts:
                contacts = [f"(policy: {policy.name})"]  # webhook/slack/teams policies with no named contacts
            for contact in contacts:
                by_contact[contact]["escalations"] += count
                by_contact[contact]["alerts"] += 1

    return {
        "window_days": days,
        "total_escalations": sum(e["escalations"] for e in daily.values()),
        "total_escalated_alerts": len(escalated_alerts),
        "by_policy": sorted(by_policy.values(), key=lambda x: -x["escalations"]),
        "by_contact": sorted(
            [{"contact": c, **v} for c, v in by_contact.items()],
            key=lambda x: -x["escalations"],
        ),
        "daily": sorted(daily.values(), key=lambda x: x["date"]),
    }


@router.post("/run-now")
def run_escalation_sweep_now(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Manually trigger an escalation sweep instead of waiting for the
    next scheduled tick -- useful right after creating/editing a policy
    to confirm it fires as expected."""
    fired = escalation_service.run_escalation_sweep(db)
    return {"escalations_fired": fired}


@router.post("", response_model=EscalationPolicyRead, status_code=201)
def create_escalation_policy(
    body: EscalationPolicyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    policy = EscalationPolicy(
        name=body.name,
        description=body.description,
        severity_scope=body.severity_scope,
        unack_minutes=body.unack_minutes,
        repeat_minutes=body.repeat_minutes,
        secondary_contacts=body.secondary_contacts,
        channel=body.channel,
        webhook_url=body.webhook_url,
        enabled=body.enabled,
        created_by=user.email,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.put("/{policy_id}", response_model=EscalationPolicyRead)
def update_escalation_policy(
    policy_id: uuid.UUID,
    body: EscalationPolicyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    policy = db.get(EscalationPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Escalation policy not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return policy


@router.delete("/{policy_id}", status_code=204)
def delete_escalation_policy(
    policy_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    policy = db.get(EscalationPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Escalation policy not found")
    db.delete(policy)
    db.commit()


@router.patch("/{policy_id}/toggle", response_model=EscalationPolicyRead)
def toggle_escalation_policy(
    policy_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    policy = db.get(EscalationPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Escalation policy not found")
    policy.enabled = not policy.enabled
    db.commit()
    db.refresh(policy)
    return policy
