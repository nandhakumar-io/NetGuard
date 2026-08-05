"""Pydantic schemas for the WebhookEndpoint CRUD API."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class WebhookCreate(BaseModel):
    name: str
    url: str
    webhook_type: str = "generic"  # generic / slack / teams / telegram
    secret: str | None = None
    events: list[str] | None = None
    telegram_chat_id: str | None = None
    enabled: bool = True


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    webhook_type: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    telegram_chat_id: str | None = None
    enabled: bool | None = None


class WebhookRead(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    webhook_type: str
    secret: str | None = None
    events: list[str] | None = None
    telegram_chat_id: str | None = None
    enabled: bool
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class WebhookTestResult(BaseModel):
    success: bool
    message: str
    status_code: int | None = None
