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
