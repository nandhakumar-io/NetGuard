"""Mobile push subscriptions -- self-scoped, like notification
preferences: any authenticated user manages their own devices, nobody
manages another user's. See app.services.push_service for delivery.
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.webhooks import _validate_outbound_url
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.push_subscription import PushProvider, PushSubscription
from app.models.user import User
from app.schemas.push_subscription import (
    PushSubscriptionCreate,
    PushSubscriptionRead,
    PushSubscriptionUpdate,
    PushTestResult,
    VapidPublicKeyResponse,
)
from app.services import push_service

router = APIRouter(prefix="/push-subscriptions", tags=["push-subscriptions"])


def _sub_to_read(sub: PushSubscription) -> PushSubscriptionRead:
    """Converts the ORM row to PushSubscriptionRead, parsing the
    include_actions JSON column -- same pattern as
    app.api.webhooks._webhook_to_read for its events/include_actions
    columns, needed because the DB stores this as a JSON-encoded Text
    column but the schema exposes it as a real list.
    """
    include_actions = None
    if sub.include_actions:
        try:
            include_actions = json.loads(sub.include_actions)
        except (ValueError, TypeError):
            include_actions = None
    return PushSubscriptionRead(
        id=sub.id,
        label=sub.label,
        provider=sub.provider.value if hasattr(sub.provider, "value") else sub.provider,
        target=sub.target,
        include_non_critical=sub.include_non_critical,
        include_actions=include_actions,
        enabled=sub.enabled,
        created_at=sub.created_at,
        last_pushed_at=sub.last_pushed_at,
    )


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
def get_vapid_public_key():
    """The public half of the server's VAPID keypair, handed to the
    frontend so it can call pushManager.subscribe({applicationServerKey}).
    `configured: false` when no keypair is set (see settings.VAPID_*) --
    the Push Notifications page uses this to hide the Browser option
    entirely rather than let someone subscribe to a feature that can't
    actually deliver anything server-side.
    """
    return VapidPublicKeyResponse(
        configured=bool(settings.VAPID_PUBLIC_KEY),
        public_key=settings.VAPID_PUBLIC_KEY,
    )


def _validate_target_if_url(provider: PushProvider, target: str) -> None:
    """ntfy's `target` is a URL the backend later httpx.posts to directly
    (see push_service._send_ntfy) -- unlike webhooks.py, this endpoint has
    no role restriction (any authenticated user manages their own push
    subscriptions), so without this check any logged-in user could point
    `target` at http://169.254.169.254/... or an internal-only service
    and use POST /push-subscriptions/{id}/test (or any real alert) to
    make the backend request it on their behalf. Pushover's `target` is
    just an opaque user key, not a URL NetGuard connects to (the fixed
    https://api.pushover.net endpoint is used instead -- see
    push_service._send_pushover), so it's exempt from this check.
    """
    if provider == PushProvider.NTFY:
        _validate_outbound_url(target, label="Push target")


class TestTargetRequest(BaseModel):
    provider: str
    target: str


@router.post("/test-target", response_model=PushTestResult)
def test_target(
    payload: TestTargetRequest, current_user: User = Depends(get_current_user)
):
    """Sends a one-off test push to a provider/target pair that hasn't
    been saved yet -- lets the Add Device dialog offer a "Send test"
    button before the subscription exists at all, instead of forcing a
    save-then-test-then-maybe-delete-and-redo round trip if the topic
    URL/user key turns out to be wrong. Never persists anything; builds
    a transient (un-added-to-session) PushSubscription just to reuse
    push_service's existing send path.
    """
    try:
        provider = PushProvider(payload.provider)
    except ValueError:
        raise HTTPException(status_code=400, detail="provider must be 'ntfy', 'pushover', or 'browser'")
    if provider == PushProvider.BROWSER:
        raise HTTPException(status_code=400, detail="Browser push can only be tested after subscribing in-browser")
    if not payload.target:
        raise HTTPException(status_code=400, detail="target is required")
    _validate_target_if_url(provider, payload.target)

    transient = PushSubscription(
        id=uuid.uuid4(), user_id=current_user.id, label="test", provider=provider, target=payload.target,
    )
    sent = push_service.send_test_push(transient)
    return PushTestResult(
        sent=sent,
        message="Test push sent — check your device." if sent else "Failed to send. Check the target/token and try again.",
    )


@router.get("", response_model=list[PushSubscriptionRead])
def list_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(PushSubscription)
        .filter(PushSubscription.user_id == current_user.id)
        .order_by(PushSubscription.created_at.desc())
        .all()
    )
    return [_sub_to_read(r) for r in rows]


@router.post("", response_model=PushSubscriptionRead, status_code=201)
def create_subscription(
    payload: PushSubscriptionCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    try:
        provider = PushProvider(payload.provider)
    except ValueError:
        raise HTTPException(status_code=400, detail="provider must be 'ntfy', 'pushover', or 'browser'")

    if provider == PushProvider.BROWSER:
        # Built server-side from the browser's own PushSubscription
        # object, not user-typed text -- no outbound-URL check needed
        # (see _validate_target_if_url's docstring for why that check
        # exists for ntfy in the first place: it's specifically about
        # trusting a *user-typed* target).
        if not payload.endpoint or not payload.p256dh or not payload.auth:
            raise HTTPException(
                status_code=400, detail="endpoint, p256dh, and auth are required for provider='browser'"
            )
        target = json.dumps({"endpoint": payload.endpoint, "p256dh": payload.p256dh, "auth": payload.auth})
    else:
        if not payload.target:
            raise HTTPException(status_code=400, detail="target is required")
        _validate_target_if_url(provider, payload.target)
        target = payload.target

    subscription = PushSubscription(
        user_id=current_user.id,
        label=payload.label,
        provider=provider,
        target=target,
        include_non_critical=payload.include_non_critical,
        include_actions=json.dumps(payload.include_actions) if payload.include_actions else None,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return _sub_to_read(subscription)


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

    updates = payload.model_dump(exclude_unset=True)
    new_provider = updates.get("provider", subscription.provider)
    if isinstance(new_provider, str):
        new_provider = PushProvider(new_provider)
    new_target = updates.get("target", subscription.target)
    if "provider" in updates or "target" in updates:
        _validate_target_if_url(new_provider, new_target)
    if "include_actions" in updates:
        updates["include_actions"] = json.dumps(updates["include_actions"]) if updates["include_actions"] else None

    for field, value in updates.items():
        setattr(subscription, field, value)

    db.commit()
    db.refresh(subscription)
    return _sub_to_read(subscription)


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
