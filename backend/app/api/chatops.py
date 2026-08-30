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
from app.core.deps import get_current_user, require_roles
from app.models.user import User, UserRole
from app.schemas.chatops import (
    ChatOpsCommandResponse,
    ChatOpsLinkCreate,
    ChatOpsLinkRead,
    ChatOpsLinkSelfCreate,
)
from app.services import audit_service, chatops_service

router = APIRouter(prefix="/chatops", tags=["chatops"])
logger = logging.getLogger(__name__)

# Slack attachment sidebar color per severity, matching the app's existing
# critical/warning/info palette (see app.models.alert.AlertSeverity).
_SEVERITY_COLOR = {
    "critical": "#DC2626",  # red
    "warning": "#F97316",  # orange
    "info": "#2563EB",  # blue
}


def _slack_blocks(result: chatops_service.ChatOpsResult) -> dict:
    """Renders a ChatOpsResult as a Slack Block Kit message: a markdown
    section block, optionally wrapped in a color-coded attachment when the
    result carries a severity, plus one 'Acknowledge' button per item that
    has an alert_id (e.g. the `alerts` command).
    """
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": result.text}}]

    for item in result.items:
        alert_id = item.get("alert_id")
        if alert_id:
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"alert_{alert_id}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Acknowledge"},
                            "action_id": "ack",
                            "value": alert_id,
                            "style": "primary",
                        }
                    ],
                }
            )
            continue

        # Rollback dry-run preview (see chatops_service._rollback) -- the
        # confirm button re-submits as `rollback_confirm <deployment-id>`,
        # which slack_interactive below maps back onto the "rollback
        # confirm <id>" text command that actually queues the rollback.
        rollback_deployment_id = item.get("rollback_deployment_id")
        if rollback_deployment_id:
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"rollback_{rollback_deployment_id}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Confirm Rollback"},
                            "action_id": "rollback_confirm",
                            "value": rollback_deployment_id,
                            "style": "danger",
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Confirm rollback"},
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "This will push the pre-deploy configuration back to the device. Proceed?",
                                },
                                "confirm": {"type": "plain_text", "text": "Roll back"},
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            },
                        }
                    ],
                }
            )

    if result.severity in _SEVERITY_COLOR:
        return {
            "response_type": "ephemeral",
            "attachments": [{"color": _SEVERITY_COLOR[result.severity], "blocks": blocks}],
        }
    return {"response_type": "ephemeral", "blocks": blocks}

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
    return _slack_blocks(result)


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
    # The rollback confirm button submits action_id="rollback_confirm" --
    # translate that back to the two-word "rollback confirm" command
    # chatops_service expects (every other button's action_id already
    # matches its command name 1:1, e.g. "ack").
    command_text = f"rollback confirm {value}".strip() if action_id == "rollback_confirm" else f"{action_id} {value}".strip()

    result = chatops_service.execute_command(db, user, command_text)
    response = _slack_blocks(result)
    response["replace_original"] = False
    return response


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


# --- Ad-hoc testing (any authenticated user, runs as themselves) ----------


@router.post("/test-command", response_model=ChatOpsCommandResponse)
def test_command(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Runs a ChatOps command through the exact same parser/executor Slack
    and Teams hit, as the calling NetGuard user -- lets an engineer verify
    a command (and its RBAC) from the API/UI before wiring it into a chat
    platform. Body: {"text": "fleet"}.
    """
    text = payload.get("text", "")
    result = chatops_service.execute_command(db, current_user, text)
    return ChatOpsCommandResponse(ok=result.ok, text=result.text, severity=result.severity, items=result.items)


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


@router.get("/links/me", response_model=ChatOpsLinkRead)
def get_my_link(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Self-service read of the caller's own link status -- unlike GET
    /links (admin-only, full roster), any authenticated user can check
    whether *their own* account is linked without needing an admin to
    look it up for them.
    """
    return ChatOpsLinkRead(
        user_id=current_user.id, user_email=current_user.email, full_name=current_user.full_name,
        slack_user_id=current_user.slack_user_id, msteams_user_id=current_user.msteams_user_id,
    )


@router.post("/links/me", response_model=ChatOpsLinkRead, status_code=201)
def link_my_account(
    payload: ChatOpsLinkSelfCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Self-service link: any authenticated user can attach their own
    Slack/Teams ID without admin involvement. Previously POST /chatops/
    links (admin-only) was the *only* way to link an account at all, so
    every single user's Slack/Teams handle had to be typed in by an admin
    by hand -- fine for a handful of users, a real bottleneck past that.
    Admin-managed /links stays as-is for linking *someone else's*
    account (onboarding on their behalf, fixing a mistyped ID, etc.).
    """
    if payload.platform not in ("slack", "teams"):
        raise HTTPException(status_code=422, detail="platform must be 'slack' or 'teams'")

    column = "slack_user_id" if payload.platform == "slack" else "msteams_user_id"
    existing = db.query(User).filter(getattr(User, column) == payload.external_user_id).first()
    if existing and existing.id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail=f"That {payload.platform} account is already linked to {existing.email}",
        )

    setattr(current_user, column, payload.external_user_id)
    db.commit()
    db.refresh(current_user)

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action=f"ChatOps Self-Link Created ({payload.platform})",
        result="Linked", detail=f"{current_user.email} self-linked their {payload.platform} account",
    )

    return ChatOpsLinkRead(
        user_id=current_user.id, user_email=current_user.email, full_name=current_user.full_name,
        slack_user_id=current_user.slack_user_id, msteams_user_id=current_user.msteams_user_id,
    )


@router.delete("/links/me", status_code=204)
def unlink_my_account(platform: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if platform not in ("slack", "teams"):
        raise HTTPException(status_code=422, detail="platform must be 'slack' or 'teams'")
    column = "slack_user_id" if platform == "slack" else "msteams_user_id"
    setattr(current_user, column, None)
    db.commit()

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action=f"ChatOps Self-Link Removed ({platform})",
        result="Unlinked", detail=f"{current_user.email} unlinked their own {platform} account",
    )


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
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action=f"ChatOps Link Created ({payload.platform})",
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
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action=f"ChatOps Link Removed ({platform})",
        result="Unlinked", detail=f"Unlinked {platform} account from {target.email}",
    )
