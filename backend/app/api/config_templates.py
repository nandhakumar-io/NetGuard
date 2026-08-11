import datetime
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.config_template import (
    ConfigTemplate,
    ConfigTemplateVersion,
    TemplateVersionStatus,
)
from app.models.git_repo_config import GitRepoConfig, GitSyncDirection, GitSyncStatus
from app.models.golden_config import GoldenConfig
from app.models.user import User, UserRole
from app.schemas.config_template import (
    ConfigTemplateCreate,
    ConfigTemplateRead,
    ConfigTemplateUpdate,
    ConfigTemplateVersionRead,
    TemplateDiffPreviewRequest,
    TemplateDiffPreviewResponse,
    TemplateRenderRequest,
    TemplateRenderResponse,
    TemplateVersionReview,
    TemplateVersionSubmit,
)
from app.services import (
    config_format_service,
    diff_engine,
    git_sync_service,
    snapshot_service,
    template_service,
)

router = APIRouter(prefix="/config-templates", tags=["config-templates"])

# Same rationale as devices.py's INVENTORY_MANAGER_ROLES -- templates
# shape what gets pushed to production devices, so authoring/editing them
# is a Network Admin action; everyone authenticated can read/render them
# (an operator drafting a change request needs to render a template, not
# necessarily edit the library).
TEMPLATE_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _attach_published_version(db: Session, row: ConfigTemplate) -> ConfigTemplate:
    """Hangs the published ConfigTemplateVersion row (if any) off the
    ConfigTemplate instance as a private attribute so
    ConfigTemplateRead.from_orm_row can surface its version_number
    without every caller having to join/query separately."""
    row._published_version_row = (
        db.get(ConfigTemplateVersion, row.published_version_id) if row.published_version_id else None
    )
    return row


def _get_template(db: Session, template_id: uuid.UUID) -> ConfigTemplate:
    row = db.get(ConfigTemplate, template_id)
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return row


@router.get("", response_model=list[ConfigTemplateRead])
def list_templates(
    device_role: str | None = Query(None, description="Filter to templates matching this device_role (or role-agnostic ones)"),
    vendor: str | None = Query(None, description="Filter to templates matching this vendor (or vendor-agnostic ones)"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = db.query(ConfigTemplate)
    if device_role:
        query = query.filter((ConfigTemplate.device_role == device_role) | (ConfigTemplate.device_role.is_(None)))
    if vendor:
        query = query.filter((ConfigTemplate.vendor == vendor) | (ConfigTemplate.vendor.is_(None)))
    rows = query.order_by(ConfigTemplate.name).all()
    return [ConfigTemplateRead.from_orm_row(_attach_published_version(db, r)) for r in rows]


@router.post("", response_model=ConfigTemplateRead, status_code=201)
def create_template(
    payload: ConfigTemplateCreate, db: Session = Depends(get_db), current_user: User = Depends(TEMPLATE_MANAGER_ROLES)
):
    row = ConfigTemplate(
        name=payload.name, description=payload.description, device_role=payload.device_role, vendor=payload.vendor,
        body=payload.body, variables=json.dumps([v.model_dump() for v in payload.variables]),
        created_by=current_user.email,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"A template named '{payload.name}' already exists.")
    db.refresh(row)
    return ConfigTemplateRead.from_orm_row(_attach_published_version(db, row))


@router.get("/{template_id}", response_model=ConfigTemplateRead)
def get_template(template_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    row = _get_template(db, template_id)
    return ConfigTemplateRead.from_orm_row(_attach_published_version(db, row))


@router.patch("/{template_id}", response_model=ConfigTemplateRead)
def update_template(
    template_id: uuid.UUID, payload: ConfigTemplateUpdate, db: Session = Depends(get_db),
    _=Depends(TEMPLATE_MANAGER_ROLES),
):
    """Edits the live *draft* only. This never touches
    published_version_id or any existing ConfigTemplateVersion row -- an
    approved version stays exactly as it was approved, byte for byte, no
    matter how many more times the draft is edited afterwards. To make an
    edit official, submit it as a new version (see
    POST /{template_id}/versions) and get it approved.
    """
    row = _get_template(db, template_id)
    updates = payload.model_dump(exclude_unset=True)
    if "variables" in updates:
        variables = updates.pop("variables")
        row.variables = json.dumps(variables) if variables is not None else None
    for field, value in updates.items():
        setattr(row, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"A template named '{payload.name}' already exists.")
    db.refresh(row)
    return ConfigTemplateRead.from_orm_row(_attach_published_version(db, row))


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(TEMPLATE_MANAGER_ROLES)):
    row = _get_template(db, template_id)
    db.delete(row)
    db.commit()


def _resolve_body_and_variables(
    db: Session, row: ConfigTemplate, version_id: uuid.UUID | None
) -> tuple[str, list[dict], ConfigTemplateVersion | None]:
    """Returns (body, declared_variables, version_row_or_None) for either
    a specific pinned version or the live draft when version_id is None."""
    if version_id is None:
        declared: list[dict] = []
        if row.variables:
            try:
                declared = json.loads(row.variables)
            except (ValueError, TypeError):
                declared = []
        return row.body, declared, None

    version = db.get(ConfigTemplateVersion, version_id)
    if not version or version.template_id != row.id:
        raise HTTPException(status_code=404, detail="Template version not found")
    declared = []
    if version.variables:
        try:
            declared = json.loads(version.variables)
        except (ValueError, TypeError):
            declared = []
    return version.body, declared, version


def _render(db: Session, row: ConfigTemplate, variables_in: dict, version_id: uuid.UUID | None):
    body, declared, version = _resolve_body_and_variables(db, row, version_id)

    # Apply declared defaults for any variable the caller didn't supply,
    # so a template author's sane defaults (e.g. a standard MTU) don't
    # have to be re-typed by every operator on every use.
    variables = dict(variables_in)
    for v in declared:
        if v.get("name") not in variables and v.get("default") is not None:
            variables[v["name"]] = v["default"]

    result = template_service.render_template(body, variables)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return result.rendered, version


@router.post("/{template_id}/render", response_model=TemplateRenderResponse)
def render_template(
    template_id: uuid.UUID, payload: TemplateRenderRequest, db: Session = Depends(get_db), _=Depends(get_current_user)
):
    """Renders the template body with the supplied variables -- the
    "fill in 3 variables" step. Returns the rendered config text; the
    caller (typically the change-request form) is responsible for
    putting it into `proposed_config`, this endpoint doesn't create a
    change request itself so a template can be previewed/tweaked before
    committing to a change.

    By default renders the live draft. Pass `version_id` to instead
    render an exact approved (or any other) snapshot -- e.g. re-rendering
    what a previously-approved change request actually used.
    """
    row = _get_template(db, template_id)
    rendered, version = _render(db, row, payload.variables, payload.version_id)
    return TemplateRenderResponse(
        rendered_config=rendered,
        version_id=version.id if version else None,
        version_number=version.version_number if version else None,
    )


# --- Versioning / approval workflow -------------------------------------


@router.get("/{template_id}/versions", response_model=list[ConfigTemplateVersionRead])
def list_template_versions(template_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_template(db, template_id)
    rows = (
        db.query(ConfigTemplateVersion)
        .filter(ConfigTemplateVersion.template_id == template_id)
        .order_by(ConfigTemplateVersion.version_number.desc())
        .all()
    )
    return [ConfigTemplateVersionRead.from_orm_row(r) for r in rows]


@router.get("/{template_id}/versions/{version_id}", response_model=ConfigTemplateVersionRead)
def get_template_version(
    template_id: uuid.UUID, version_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)
):
    version = db.get(ConfigTemplateVersion, version_id)
    if not version or version.template_id != template_id:
        raise HTTPException(status_code=404, detail="Template version not found")
    return ConfigTemplateVersionRead.from_orm_row(version)


@router.post("/{template_id}/versions", response_model=ConfigTemplateVersionRead, status_code=201)
def submit_template_version(
    template_id: uuid.UUID, payload: TemplateVersionSubmit, db: Session = Depends(get_db),
    current_user: User = Depends(TEMPLATE_MANAGER_ROLES),
):
    """Freezes the current draft (ConfigTemplate.body/variables) as a new,
    immutable version and puts it up for approval. Editing the draft
    further afterwards has no effect on this snapshot."""
    row = _get_template(db, template_id)
    last = (
        db.query(ConfigTemplateVersion)
        .filter(ConfigTemplateVersion.template_id == template_id)
        .order_by(ConfigTemplateVersion.version_number.desc())
        .first()
    )
    next_number = (last.version_number + 1) if last else 1
    version = ConfigTemplateVersion(
        template_id=template_id, version_number=next_number,
        body=row.body, variables=row.variables,
        status=TemplateVersionStatus.PENDING_APPROVAL.value,
        change_note=payload.change_note, submitted_by=current_user.email,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return ConfigTemplateVersionRead.from_orm_row(version)


@router.post("/{template_id}/versions/{version_id}/approve", response_model=ConfigTemplateVersionRead)
def approve_template_version(
    template_id: uuid.UUID, version_id: uuid.UUID, payload: TemplateVersionReview, db: Session = Depends(get_db),
    current_user: User = Depends(TEMPLATE_MANAGER_ROLES),
):
    version = db.get(ConfigTemplateVersion, version_id)
    if not version or version.template_id != template_id:
        raise HTTPException(status_code=404, detail="Template version not found")
    if version.status != TemplateVersionStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=400, detail=f"Version is '{version.status}', not pending approval.")
    if version.submitted_by == current_user.email:
        # Same rationale as change-request approval (self-approval defeats
        # the point of a review step) -- see change_requests.py.
        raise HTTPException(status_code=400, detail="You can't approve your own submitted version.")

    version.status = TemplateVersionStatus.PUBLISHED.value
    version.reviewed_by = current_user.email
    version.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
    version.review_note = payload.note

    row = _get_template(db, template_id)
    row.published_version_id = version.id

    db.commit()
    db.refresh(version)

    # GitOps mirror (config-as-code): best-effort push of the newly
    # published version to every PUSH/BIDIRECTIONAL repo. Never blocks or
    # fails the approval itself -- NetGuard's own record of what's
    # published is authoritative regardless of whether the repo mirror
    # commit succeeds; a failure is recorded on the repo row (surfaced in
    # the GitOps settings page) rather than raised here.
    push_repos = (
        db.query(GitRepoConfig)
        .filter(GitRepoConfig.direction.in_([GitSyncDirection.PUSH, GitSyncDirection.BIDIRECTIONAL]))
        .all()
    )
    for repo in push_repos:
        try:
            git_sync_service.push_template_version(db, repo, row, version)
            repo.last_sync_status = GitSyncStatus.SUCCEEDED
            repo.last_sync_error = None
        except git_sync_service.GitSyncError as exc:
            repo.last_sync_status = GitSyncStatus.FAILED
            repo.last_sync_error = str(exc)
        db.commit()

    return ConfigTemplateVersionRead.from_orm_row(version)


@router.post("/{template_id}/versions/{version_id}/reject", response_model=ConfigTemplateVersionRead)
def reject_template_version(
    template_id: uuid.UUID, version_id: uuid.UUID, payload: TemplateVersionReview, db: Session = Depends(get_db),
    current_user: User = Depends(TEMPLATE_MANAGER_ROLES),
):
    version = db.get(ConfigTemplateVersion, version_id)
    if not version or version.template_id != template_id:
        raise HTTPException(status_code=404, detail="Template version not found")
    if version.status != TemplateVersionStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=400, detail=f"Version is '{version.status}', not pending approval.")

    version.status = TemplateVersionStatus.REJECTED.value
    version.reviewed_by = current_user.email
    version.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
    version.review_note = payload.note
    db.commit()
    db.refresh(version)
    return ConfigTemplateVersionRead.from_orm_row(version)


# --- Diff preview against golden/live config ----------------------------


@router.post("/{template_id}/diff-preview", response_model=TemplateDiffPreviewResponse)
def preview_template_diff(
    template_id: uuid.UUID, payload: TemplateDiffPreviewRequest, db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Renders the template, then runs the rendered output through the
    same structural-diff machinery used for drift detection
    (config_format_service.xml_structural_diff / to_cli_commands) against
    the target device's *current* baseline, so the operator sees the
    actual delta a template produces on this specific device -- not just
    the raw rendered text in isolation.
    """
    from app.models.device import Device

    row = _get_template(db, template_id)
    rendered, _version = _render(db, row, payload.variables, payload.version_id)

    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if payload.compare_against == "live":
        from app.services.protocol_manager import ProtocolManager

        pm = ProtocolManager(db, device)
        result = pm.get_running_config()
        if not result.success:
            raise HTTPException(status_code=502, detail=result.error or "Failed to read live running config")
        base_label, base_config = "live running config", result.output
    else:
        golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
        if not golden:
            raise HTTPException(
                status_code=404,
                detail="No golden config set for this device yet -- set one, or pass compare_against=\"live\".",
            )
        base_label, base_config = "golden config", snapshot_service.decrypt_config(golden.config_encrypted)

    identical = base_config.strip() == rendered.strip()

    # Same reasoning as compare_config: diff on pretty-printed text when
    # either side is XML, or the line diff degenerates into "everything
    # changed" on unindented XML.
    diff_base = config_format_service.pretty_xml(base_config) or base_config
    diff_target = config_format_service.pretty_xml(rendered) or rendered
    diff = diff_engine.generate_diff(diff_base, diff_target)

    structural_changes = config_format_service.xml_structural_diff(base_config, rendered)
    cli_diff = config_format_service.to_cli_commands(structural_changes) if structural_changes else []
    change_summary = (
        config_format_service.humanize_structural_diff(structural_changes) if structural_changes else []
    )

    return TemplateDiffPreviewResponse(
        rendered_config=rendered, base_label=base_label, identical=identical, diff=diff,
        cli_diff=cli_diff, change_summary=change_summary,
    )
