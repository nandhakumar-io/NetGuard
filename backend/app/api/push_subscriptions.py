"""Mobile push subscriptions -- self-scoped, like notification
preferences: any authenticated user manages their own devices, nobody
manages another user's. See app.services.push_service for delivery.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.push_subscription import PushProvider, PushSubscription
from app.models.user import User
from app.schemas.push_subscription import (
    PushSubscriptionCreate,
    PushSubscriptionRead,
    PushSubscriptionUpdate,
    PushTestResult,
)
from app.services import push_service

router = APIRouter(prefix="/push-subscriptions", tags=["push-subscriptions"])


@router.get("", response_model=list[PushSubscriptionRead])
def list_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return (
        db.query(PushSubscription)
        .filter(PushSubscription.user_id == current_user.id)
        .order_by(PushSubscription.created_at.desc())
        .all()
    )


@router.post("", response_model=PushSubscriptionRead, status_code=201)
def create_subscription(
    payload: PushSubscriptionCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    try:
        provider = PushProvider(payload.provider)
    except ValueError:
        raise HTTPException(status_code=400, detail="provider must be 'ntfy' or 'pushover'")

    subscription = PushSubscription(
        user_id=current_user.id,
        label=payload.label,
        provider=provider,
        target=payload.target,
        include_non_critical=payload.include_non_critical,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


@router.patch("/{subscription_id}", response_model=PushSubscriptionRead)
def update_subscription(
    subscription_id: uuid.UUID,
    payload: PushSubscriptionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    subscription = db.get(PushSubscription, subscription_id)
    if not subscription or subscription.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Push subscription not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(subscription, field, value)

    db.commit()
    db.refresh(subscription)
    return subscription


@router.delete("/{subscription_id}", status_code=204)
def delete_subscription(
    subscription_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    subscription = db.get(PushSubscription, subscription_id)
    if not subscription or subscription.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Push subscription not found")
    db.delete(subscription)
    db.commit()


@router.post("/{subscription_id}/test", response_model=PushTestResult)
def test_subscription(
    subscription_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    subscription = db.get(PushSubscription, subscription_id)
    if not subscription or subscription.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Push subscription not found")

    sent = push_service.send_test_push(subscription)
    return PushTestResult(
        sent=sent,
        message="Test push sent — check your device." if sent else "Failed to send. Check the target/token and try again.",
    )
