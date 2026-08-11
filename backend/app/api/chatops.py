"""Two-way ChatOps (FR-11 extension).

Outbound notifications already reach Slack/Teams via
app.services.notification_service. These endpoints are the inbound half:
Slack slash commands, Slack interactive button clicks, and a Microsoft
Teams outgoing-webhook, each mapped to a NetGuard user via a prior
POST /chatops/links, then executed through app.services.chatops_service
(which reuses the exact same approve/reject/rollback endpoint functions
the UI calls -- same RBAC, same audit trail).
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.user import User, UserRole
from app.schemas.chatops import ChatOpsLinkCreate, ChatOpsLinkRead
from app.services import audit_service, chatops_service

router = APIRouter(prefix="/chatops", tags=["chatops"])
logger = logging.getLogger(__name__)

# Linking a Slack/Teams identity to a NetGuard user grants that chat
# account the ability to approve changes and trigger rollbacks -- same
# blast radius as granting API access, so only a Network Admin can do it.
CHATOPS_ADMIN_ROLES = require_roles(UserRole.NETWORK_ADMIN)


# --- Slack -------------------------------------------------------------


@router.post("/slack/commands")
async def slack_slash_command(
    request: Request,
    db: Session = Depends(get_db),
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
):
    """Slack slash command handler (e.g. `/netguard approve <cr-id>`).
    Slack expects a fast, plain-text (or Block Kit JSON) response -- no
    redirects, no auth challenge -- so an unlinked user gets a helpful
    reply here rather than a 401 (Slack surfaces non-2xx responses as a
    generic "command failed" with no detail).
    """
    raw_body = await request.body()
    if not chatops_service.verify_slack_signature(raw_body, x_slack_request_timestamp, x_slack_signature):
        raise HTTPException(status_code=401, detail="Invalid Slack request signature")

    form = await request.form()
    slack_user_id = form.get("user_id")
    text = form.get("text", "")

    user = chatops_service.resolve_user(db, slack_user_id=slack_user_id)
    if not user:
        return {
            "response_type": "ephemeral",
            "text": (
                "Your Slack account isn't linked to a NetGuard user yet. "
                "Ask a Network Admin to link it via POST /chatops/links."
            ),
        }

    result = chatops_service.execute_command(db, user, text)
    return {"response_type": "ephemeral", "text": result.text}


@router.post("/slack/interactive")
async def slack_interactive(
    request: Request,
    db: Session = Depends(get_db),
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
):
    """Slack interactive component handler for Approve/Reject buttons
    attached to a change-request notification message. Slack posts the
    button's `action_id` ("approve" / "reject") and `value` (the change
    request ID) as a JSON-encoded `payload` form field.
    """
    raw_body = await request.body()
    if not chatops_service.verify_slack_signature(raw_body, x_slack_request_timestamp, x_slack_signature):
        raise HTTPException(status_code=401, detail="Invalid Slack request signature")

    form = await request.form()
    try:
        payload = json.loads(form.get("payload", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed interactive payload")

    slack_user_id = (payload.get("user") or {}).get("id")
    user = chatops_service.resolve_user(db, slack_user_id=slack_user_id)
    if not user:
        return {
            "response_type": "ephemeral",
            "text": "Your Slack account isn't linked to a NetGuard user yet. Ask a Network Admin to link it.",
        }

    actions = payload.get("actions") or []
    if not actions:
        return {"response_type": "ephemeral", "text": "No action received."}

    action_id = actions[0].get("action_id", "")
    value = actions[0].get("value", "")
    command_text = f"{action_id} {value}".strip()

    result = chatops_service.execute_command(db, user, command_text)
    return {"response_type": "ephemeral", "text": result.text, "replace_original": False}


# --- Microsoft Teams -----------------------------------------------------


@router.post("/teams/commands")
async def teams_outgoing_webhook(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Microsoft Teams "Outgoing Webhook" handler. Teams posts an
    Activity payload with `text` (the message, bot mention already
    stripped by Teams) and `from.id` (the sender's Teams AAD object ID).
    Reply shape is Teams' plain "simple text reply" Activity.
    """
    raw_body = await request.body()
    if not chatops_service.verify_teams_hmac(raw_body, authorization):
        raise HTTPException(status_code=401, detail="Invalid Teams HMAC signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed Teams payload")

    teams_user_id = (payload.get("from") or {}).get("id")
    text = payload.get("text", "")

    user = chatops_service.resolve_user(db, msteams_user_id=teams_user_id)
    if not user:
        return {
            "type": "message",
            "text": "Your Teams account isn't linked to a NetGuard user yet. Ask a Network Admin to link it.",
        }

    result = chatops_service.execute_command(db, user, text)
    return {"type": "message", "text": result.text}


# --- Link management (Network Admin only) ---------------------------------


@router.get("/links", response_model=list[ChatOpsLinkRead])
def list_links(db: Session = Depends(get_db), _=Depends(CHATOPS_ADMIN_ROLES)):
    users = db.query(User).filter((User.slack_user_id.isnot(None)) | (User.msteams_user_id.isnot(None))).all()
    return [
        ChatOpsLinkRead(
            user_id=u.id, user_email=u.email, full_name=u.full_name,
            slack_user_id=u.slack_user_id, msteams_user_id=u.msteams_user_id,
        )
        for u in users
    ]


@router.post("/links", response_model=ChatOpsLinkRead, status_code=201)
def create_link(
    payload: ChatOpsLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(CHATOPS_ADMIN_ROLES),
):
    if payload.platform not in ("slack", "teams"):
        raise HTTPException(status_code=422, detail="platform must be 'slack' or 'teams'")

    target = db.query(User).filter(User.email == payload.user_email).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"No NetGuard user with email {payload.user_email}")

    column = "slack_user_id" if payload.platform == "slack" else "msteams_user_id"
    existing = db.query(User).filter(getattr(User, column) == payload.external_user_id).first()
    if existing and existing.id != target.id:
        raise HTTPException(
            status_code=409,
            detail=f"That {payload.platform} account is already linked to {existing.email}",
        )

    setattr(target, column, payload.external_user_id)
    db.commit()
    db.refresh(target)

    audit_service.record_event(
        db, actor=current_user.email, action=f"ChatOps Link Created ({payload.platform})",
        result="Linked", detail=f"Linked {payload.platform} account to {target.email}",
    )

    return ChatOpsLinkRead(
        user_id=target.id, user_email=target.email, full_name=target.full_name,
        slack_user_id=target.slack_user_id, msteams_user_id=target.msteams_user_id,
    )


@router.delete("/links/{user_id}", status_code=204)
def delete_link(
    user_id: uuid.UUID, platform: str, db: Session = Depends(get_db), current_user: User = Depends(CHATOPS_ADMIN_ROLES)
):
    if platform not in ("slack", "teams"):
        raise HTTPException(status_code=422, detail="platform must be 'slack' or 'teams'")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    column = "slack_user_id" if platform == "slack" else "msteams_user_id"
    setattr(target, column, None)
    db.commit()

    audit_service.record_event(
        db, actor=current_user.email, action=f"ChatOps Link Removed ({platform})",
        result="Unlinked", detail=f"Unlinked {platform} account from {target.email}",
    )
