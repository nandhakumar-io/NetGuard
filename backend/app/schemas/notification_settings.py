"""Pydantic schemas for the DB-backed notification settings (SMTP email)
API -- see app.models.notification_settings.
"""
from datetime import datetime

from pydantic import BaseModel


class NotificationSettingsRead(BaseModel):
    smtp_enabled: bool
    smtp_host: str | None = None
    smtp_port: int
    smtp_username: str | None = None
    # Never echoes the password back -- only whether one is set, same
    # posture as every other secret field in this codebase (device SSH/
    # SNMP credentials are also write-only from the API's perspective).
    smtp_password_set: bool
    smtp_from_email: str | None = None
    smtp_use_tls: bool
    recipients: str | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class NotificationSettingsUpdate(BaseModel):
    smtp_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    # Write-only. Omit/None = leave the stored password unchanged; pass
    # an empty string "" to explicitly clear it.
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool | None = None
    recipients: str | None = None


class NotificationTestResult(BaseModel):
    success: bool
    detail: str
