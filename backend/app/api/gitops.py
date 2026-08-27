"""Config-as-code / GitOps workflow (FR extension).

Config templates already have a draft -> pending_approval -> published
lifecycle (see app.models.config_template). This module adds a Git
repository as an alternate way to *propose* a new version -- a push or
merged PR on the watched branch pulls the changed template files in and
queues them for the exact same in-app review/approval step a human
editing the template directly would go through -- plus an optional push
of published versions back out to the repo so it stays a live mirror.
"""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope, require_roles
from app.models.git_repo_config import GitRepoConfig, GitSyncDirection
from app.models.user import User, UserRole
from app.schemas.gitops import (
    GitRepoConfigCreate,
    GitRepoConfigRead,
    GitRepoConfigUpdate,
    GitSyncTriggerResult,
)
from app.services import audit_service, git_sync_service

router = APIRouter(prefix="/gitops", tags=["gitops"])

# A repo config carries write-scoped credentials (PUSH/BIDIRECTIONAL) and
# controls what gets auto-submitted into the template review queue -- same
# blast radius as authoring templates directly, so Network Admin only.
GITOPS_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _get_repo(db: Session, repo_id: uuid.UUID, tenant_id) -> GitRepoConfig:
    """Fetches a GitRepoConfig and enforces tenant ownership -- a repo
    with tenant_id=None is an MSP-staff-authored/global config, visible
    to every tenant but only editable by MSP staff (GITOPS_MANAGER_ROLES
    doesn't currently distinguish that further, same posture as
    app.api.network_discovery's scans/schedules)."""
    repo = db.get(GitRepoConfig, repo_id)
    if not repo or (tenant_id is not None and repo.tenant_id not in (None, tenant_id)):
        raise HTTPException(status_code=404, detail="Git repo config not found")
    return repo


@router.get("/repos", response_model=list[GitRepoConfigRead])
def list_repos(db: Session = Depends(get_db), _=Depends(get_current_user), tenant_id=Depends(get_tenant_scope)):
    q = db.query(GitRepoConfig)
    if tenant_id is not None:
        q = q.filter((GitRepoConfig.tenant_id == tenant_id) | (GitRepoConfig.tenant_id.is_(None)))
    return [GitRepoConfigRead.from_orm_row(r) for r in q.order_by(GitRepoConfig.name).all()]


@router.post("/repos", response_model=GitRepoConfigRead, status_code=201)
def create_repo(
    payload: GitRepoConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(GITOPS_MANAGER_ROLES),
    tenant_id=Depends(get_tenant_scope),
):
    existing = db.query(GitRepoConfig).filter(GitRepoConfig.name == payload.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A repo config named '{payload.name}' already exists")

    row = GitRepoConfig(
        tenant_id=tenant_id,
        name=payload.name, repo_url=payload.repo_url, branch=payload.branch,
        template_path=payload.template_path, direction=GitSyncDirection(payload.direction),
        auto_sync_enabled=payload.auto_sync_enabled,
        access_token_encrypted=crypto.encrypt(payload.access_token) if payload.access_token else None,
        webhook_secret_encrypted=crypto.encrypt(payload.webhook_secret) if payload.webhook_secret else None,
        created_by=current_user.email,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    audit_service.record_event(
        db, actor=current_user.email, action="GitOps Repo Added", result="Created",
        detail=f"{row.name} ({row.repo_url}, branch {row.branch}, direction {row.direction.value})",
    )
    return GitRepoConfigRead.from_orm_row(row)


@router.patch("/repos/{repo_id}", response_model=GitRepoConfigRead)
def update_repo(
    repo_id: uuid.UUID, payload: GitRepoConfigUpdate, db: Session = Depends(get_db),
    current_user: User = Depends(GITOPS_MANAGER_ROLES),
    tenant_id=Depends(get_tenant_scope),
):
    row = _get_repo(db, repo_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)

    if "access_token" in data:
        token = data.pop("access_token")
        row.access_token_encrypted = crypto.encrypt(token) if token else None
    if "webhook_secret" in data:
        secret = data.pop("webhook_secret")
        row.webhook_secret_encrypted = crypto.encrypt(secret) if secret else None
    if "direction" in data and data["direction"] is not None:
        data["direction"] = GitSyncDirection(data["direction"])

    for key, value in data.items():
        setattr(row, key, value)

    db.commit()
    db.refresh(row)
    audit_service.record_event(
        db, actor=current_user.email, action="GitOps Repo Updated", result="Updated", detail=row.name
    )
    return GitRepoConfigRead.from_orm_row(row)


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(
    repo_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(GITOPS_MANAGER_ROLES),
    tenant_id=Depends(get_tenant_scope),
):
    row = _get_repo(db, repo_id, tenant_id)
    db.delete(row)
    db.commit()
    audit_service.record_event(
        db, actor=current_user.email, action="GitOps Repo Removed", result="Deleted", detail=row.name
    )


@router.post("/repos/{repo_id}/sync", response_model=GitSyncTriggerResult)
def trigger_sync(
    repo_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(GITOPS_MANAGER_ROLES),
    tenant_id=Depends(get_tenant_scope),
):
    """Manual "sync now" -- runs inline (clone/fetch of one repo is
    typically a few seconds) rather than via Celery, so the operator gets
    an immediate created/updated/unchanged/errors summary instead of
    having to poll."""
    row = _get_repo(db, repo_id, tenant_id)
    result = git_sync_service.sync_repo(db, row)
    audit_service.record_event(
        db, actor=current_user.email, action="GitOps Manual Sync", result=row.last_sync_status.value,
        detail=f"{row.name}: +{result['created']} created, {result['updated']} updated, "
               f"{result['unchanged']} unchanged, {len(result['errors'])} errors",
    )
    return GitSyncTriggerResult(status=row.last_sync_status.value, **result)


@router.post("/webhook/{repo_id}", status_code=202)
async def receive_webhook(
    repo_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Inbound push/PR-merge webhook (GitHub convention: `X-Hub-Signature-256`
    HMAC over the raw body, using the repo's configured webhook secret).
    GitLab/Bitbucket point-and-click webhooks can be adapted to the same
    URL by configuring them to send the same signature header, or by
    swapping in their own verification scheme here.

    Runs the sync as a background task so the webhook sender gets a fast
    202 rather than waiting out a full clone/fetch -- most Git hosts
    enforce a short webhook response timeout and will mark the delivery
    failed (and retry) if it's slow, even though the sync itself
    eventually succeeds.
    """
    # Unauthenticated external caller (the Git host), not a logged-in
    # user -- no tenant_id to scope against here. Trust is established by
    # the HMAC signature check below, not by tenant membership, same as
    # any other webhook receiver in this codebase.
    row = db.get(GitRepoConfig, repo_id)
    if not row:
        raise HTTPException(status_code=404, detail="Git repo config not found")
    if not row.webhook_secret_encrypted:
        raise HTTPException(status_code=409, detail="This repo has no webhook secret configured")

    secret = crypto.decrypt(row.webhook_secret_encrypted)
    body = await request.body()
    if not secret or not git_sync_service.verify_github_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    # Only a push to the watched branch (or an unrecognized/simple ping
    # payload, which we sync on anyway rather than trying to be clever)
    # triggers a sync -- a push to an unrelated branch shouldn't queue
    # template reviews for changes that were never merged to `branch`.
    ref = payload.get("ref", "")
    if ref and ref != f"refs/heads/{row.branch}":
        return {"status": "ignored", "reason": f"push was to {ref}, not refs/heads/{row.branch}"}

    from app.core.database import SessionLocal

    def _run_sync():
        bg_db = SessionLocal()
        try:
            bg_repo = bg_db.get(GitRepoConfig, repo_id)
            if bg_repo:
                git_sync_service.sync_repo(bg_db, bg_repo)
        finally:
            bg_db.close()

    background_tasks.add_task(_run_sync)
    return {"status": "accepted"}
