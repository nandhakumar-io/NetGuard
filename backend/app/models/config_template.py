import enum
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class TemplateVersionStatus(str, enum.Enum):
    """Lifecycle of a single ConfigTemplateVersion snapshot.

    DRAFT -> PENDING_APPROVAL -> PUBLISHED (or REJECTED, which just parks
    the snapshot -- the author edits the live draft and submits again as
    a new version rather than the rejected one being resurrected).
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ConfigTemplate(Base):
    """A reusable Jinja2 config-provisioning template (e.g. "standard
    access-switch template"), keyed off `device_role` so it's easy to
    surface "here are the templates that apply to this device" instead
    of every operator hand-writing/pasting CLI or NETCONF XML from
    scratch on every change.

    `body` is the raw Jinja2 template text -- CLI or XML, either is just
    text to Jinja2 -- containing `{{ variable }}` placeholders (e.g.
    `{{ vlan_id }}`, `{{ uplink_ip }}`). `variables` is a JSON-encoded
    list of variable *definitions* (name/label/default/required) so the
    UI can render a real form instead of asking the operator to intuit
    variable names by reading the template body.

    Rendering itself lives in app.services.template_service (kept
    separate from this model so the Jinja2 dependency + sandboxing
    concerns don't leak into the ORM layer).
    """

    __tablename__ = "config_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    # Matches Device.device_role (e.g. "access", "core", "distribution",
    # "edge") -- None means "applies to any role", not "applies to none".
    device_role = Column(String, nullable=True, index=True)
    # Matches Device.vendor.value (e.g. "cisco") -- None means "any vendor".
    vendor = Column(String, nullable=True, index=True)

    body = Column(Text, nullable=False)
    # JSON-encoded list of {"name", "label", "default", "required"} dicts,
    # in the order they should be presented to the operator. Kept
    # explicit (rather than only inferring names from the template body
    # via jinja2.meta) so a template author can attach a human label and
    # a sane default per variable -- inference alone can recover names,
    # not intent.
    variables = Column(Text, nullable=True)

    created_by = Column(String, nullable=False, default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # The currently-approved snapshot a change request should pin to.
    # `body`/`variables` above remain the *live editable draft* -- editing
    # them (PATCH) no longer changes what's considered "the template" for
    # audit purposes; only publishing a version (see ConfigTemplateVersion)
    # moves this pointer. None means nothing has ever been approved yet.
    published_version_id = Column(UUID(as_uuid=True), ForeignKey("config_template_versions.id"), nullable=True)


class ConfigTemplateVersion(Base):
    """An immutable, point-in-time snapshot of a ConfigTemplate's body +
    variables, carried through a draft -> pending_approval -> published
    (or rejected) workflow.

    Editing a template's live draft (ConfigTemplate.body) in place used to
    be the *only* representation -- fine for iterating, but wrong for
    anything audited: a change request built against "the template" had no
    way to say which exact wording was reviewed and approved, since the
    template could be edited out from under it at any time. Submitting a
    version freezes body/variables at that moment; approving it just flips
    a status flag and points ConfigTemplate.published_version_id at this
    row -- the row's content itself never changes again.
    """

    __tablename__ = "config_template_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id = Column(UUID(as_uuid=True), ForeignKey("config_templates.id"), nullable=False, index=True)
    # Monotonically increasing per template (1, 2, 3, ...) -- assigned at
    # submission time, not editable, so "v3" always means the same thing.
    version_number = Column(Integer, nullable=False)

    body = Column(Text, nullable=False)
    variables = Column(Text, nullable=True)

    status = Column(String, nullable=False, default=TemplateVersionStatus.PENDING_APPROVAL.value)
    change_note = Column(Text, nullable=True)

    submitted_by = Column(String, nullable=False)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())

    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)
