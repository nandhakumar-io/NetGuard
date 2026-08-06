import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class TemplateVariable(BaseModel):
    name: str
    label: str | None = None
    default: str | None = None
    required: bool = True


class ConfigTemplateBase(BaseModel):
    name: str
    description: str | None = None
    device_role: str | None = None
    vendor: str | None = None
    body: str
    variables: list[TemplateVariable] = []


class ConfigTemplateCreate(ConfigTemplateBase):
    pass


class ConfigTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    device_role: str | None = None
    vendor: str | None = None
    body: str | None = None
    variables: list[TemplateVariable] | None = None


class ConfigTemplateRead(ConfigTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_by: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # None means nothing has ever been approved for this template yet --
    # the draft (`body`/`variables` above) has no audited counterpart, so
    # any change request built off it can't cite an approved version.
    published_version_id: uuid.UUID | None = None
    published_version_number: int | None = None

    @classmethod
    def from_orm_row(cls, row) -> "ConfigTemplateRead":
        import json

        raw = row.variables
        variables: list[dict] = []
        if raw:
            try:
                variables = json.loads(raw)
            except (ValueError, TypeError):
                variables = []
        published_version_number = None
        published = getattr(row, "_published_version_row", None)
        if published is not None:
            published_version_number = published.version_number
        return cls.model_construct(
            id=row.id, name=row.name, description=row.description,
            device_role=row.device_role, vendor=row.vendor, body=row.body,
            variables=[TemplateVariable(**v) for v in variables],
            created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
            published_version_id=row.published_version_id,
            published_version_number=published_version_number,
        )


class TemplateRenderRequest(BaseModel):
    variables: dict[str, str] = {}
    # Pin rendering to a specific approved version's body instead of the
    # live (possibly-since-edited) draft -- e.g. a change request that was
    # built against v3 should still render v3 even if someone has since
    # started editing v4's draft.
    version_id: uuid.UUID | None = None


class TemplateRenderResponse(BaseModel):
    rendered_config: str
    # Which version was actually rendered, if any (None => the live draft).
    version_id: uuid.UUID | None = None
    version_number: int | None = None


# --- Template versioning / approval workflow ---------------------------


class TemplateVersionSubmit(BaseModel):
    """Freezes the template's current draft body/variables as a new,
    immutable version and puts it up for approval."""

    change_note: str | None = None


class TemplateVersionReview(BaseModel):
    note: str | None = None


class ConfigTemplateVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    version_number: int
    body: str
    variables: list[TemplateVariable] = []
    status: str
    change_note: str | None = None
    submitted_by: str
    submitted_at: datetime.datetime
    reviewed_by: str | None = None
    reviewed_at: datetime.datetime | None = None
    review_note: str | None = None

    @classmethod
    def from_orm_row(cls, row) -> "ConfigTemplateVersionRead":
        import json

        raw = row.variables
        variables: list[dict] = []
        if raw:
            try:
                variables = json.loads(raw)
            except (ValueError, TypeError):
                variables = []
        return cls.model_construct(
            id=row.id, template_id=row.template_id, version_number=row.version_number,
            body=row.body, variables=[TemplateVariable(**v) for v in variables],
            status=row.status, change_note=row.change_note,
            submitted_by=row.submitted_by, submitted_at=row.submitted_at,
            reviewed_by=row.reviewed_by, reviewed_at=row.reviewed_at, review_note=row.review_note,
        )


# --- Diff preview against a device's golden/current config -------------


class TemplateDiffPreviewRequest(BaseModel):
    device_id: uuid.UUID
    variables: dict[str, str] = {}
    version_id: uuid.UUID | None = None
    # What to diff the rendered output against. "golden" (default) uses
    # the device's approved GoldenConfig; "live" reads the device's
    # current running config over the wire instead.
    compare_against: str = "golden"


class TemplateDiffPreviewResponse(BaseModel):
    rendered_config: str
    base_label: str
    identical: bool
    diff: str
    cli_diff: list[str] = []
    change_summary: list[str] = []
