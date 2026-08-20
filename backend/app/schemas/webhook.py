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
    # Subset of ["acknowledge", "escalate", "run_runbook"] -- response
    # actions to attach to each delivery (interactive buttons on Slack/
    # Teams, inline keyboard on Telegram, an "actions" array of deep
    # links for generic endpoints).
    include_actions: list[str] | None = None
    # Runbook the "run_runbook" action links to when the triggering
    # alert has no runbook of its own suggested. None = fall back to
    # linking the general runbooks page.
    default_runbook_id: uuid.UUID | None = None
    enabled: bool = True


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    webhook_type: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    telegram_chat_id: str | None = None
    include_actions: list[str] | None = None
    default_runbook_id: uuid.UUID | None = None
    enabled: bool | None = None


class WebhookRead(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    webhook_type: str
    secret: str | None = None
    events: list[str] | None = None
    telegram_chat_id: str | None = None
    include_actions: list[str] | None = None
    default_runbook_id: uuid.UUID | None = None
    default_runbook_name: str | None = None
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


class WebhookDeliveryRead(BaseModel):
    id: uuid.UUID
    webhook_endpoint_id: uuid.UUID
    webhook_endpoint_name: str | None = None
    event: str
    event_type: str | None = None
    severity: str | None = None
    success: bool
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None
    is_retry: bool
    retry_of_id: uuid.UUID | None = None
    retried_by: str | None = None
    attempted_at: datetime

    class Config:
        from_attributes = True
