import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class ChatOpsLinkCreate(BaseModel):
    platform: str  # "slack" | "teams"
    external_user_id: str
    user_email: str


class ChatOpsLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    user_email: str
    full_name: str
    slack_user_id: str | None = None
    msteams_user_id: str | None = None


class ChatOpsCommandLogEntry(BaseModel):
    actor: str
    action: str
    result: str
    detail: str | None = None
    created_at: datetime.datetime | None = None


class ChatOpsCommandItem(BaseModel):
    """One structured row backing a ChatOpsCommandResponse -- e.g. a single
    alert returned by `alerts <hostname>`. Deliberately loose (all optional
    besides an identifying key) since different commands surface different
    fields; the Integrations page UI picks out what it needs by key."""

    alert_id: str | None = None
    hostname: str | None = None
    severity: str | None = None
    category: str | None = None


class ChatOpsCommandResponse(BaseModel):
    """Structured counterpart to the plain-text ChatOps reply. The text
    reply is always what's shown in Slack/Teams; this optional payload
    lets the same command result be consumed by the NetGuard UI (e.g. a
    future 'run a ChatOps command' panel on the Integrations page)
    without scraping the text."""

    ok: bool
    text: str
    severity: str | None = None
    items: list[ChatOpsCommandItem] = []
