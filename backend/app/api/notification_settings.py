"""Notification Settings API -- DB-backed SMTP email configuration,
surfaced on the Integrations page instead of only being settable via
env vars at deploy time. See app.models.notification_settings.

  GET   /notification-settings         — read current config (password never echoed)
  PUT   /notification-settings         — update config
  POST  /notification-settings/test    — send a test email to the configured recipients

Write access restricted to NETWORK_ADMIN, same posture as
app.api.webhooks -- this stores SMTP credentials, which is exactly the
kind of thing that shouldn't be editable by every authenticated user.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.database import get_db
from app.core.deps import require_roles
from app.models.notification_settings import SETTINGS_ROW_ID, NotificationSettings
from app.models.user import User, UserRole
from app.schemas.notification_settings import (
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    NotificationTestResult,
)
from app.services import notification_service

router = APIRouter(prefix="/notification-settings", tags=["notification-settings"])

_notification_admin = require_roles(UserRole.NETWORK_ADMIN)


def _get_or_create(db: Session) -> NotificationSettings:
    row = db.get(NotificationSettings, SETTINGS_ROW_ID)
    if row is None:
        row = NotificationSettings(id=SETTINGS_ROW_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _to_read(row: NotificationSettings) -> NotificationSettingsRead:
    return NotificationSettingsRead(
        smtp_enabled=row.smtp_enabled,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        smtp_username=row.smtp_username,
        smtp_password_set=bool(row.smtp_password_encrypted),
        smtp_from_email=row.smtp_from_email,
        smtp_use_tls=row.smtp_use_tls,
        recipients=row.recipients,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


@router.get("", response_model=NotificationSettingsRead)
def get_notification_settings(db: Session = Depends(get_db), _: User = Depends(_notification_admin)):
    return _to_read(_get_or_create(db))


@router.put("", response_model=NotificationSettingsRead)
def update_notification_settings(
    body: NotificationSettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_notification_admin),
):
    row = _get_or_create(db)
    updates = body.model_dump(exclude_unset=True)

    # Password is write-only and handled separately from the plain
    # field loop below: omitted = unchanged, "" or null = clear,
    # anything else = re-encrypt and store.
    if "smtp_password" in updates:
        new_password = updates.pop("smtp_password")
        row.smtp_password_encrypted = crypto.encrypt(new_password) if new_password else None

    for field, value in updates.items():
        setattr(row, field, value)

    row.updated_by = user.email
    db.commit()
    db.refresh(row)
    return _to_read(row)


@router.post("/test", response_model=NotificationTestResult)
def send_test_email(db: Session = Depends(get_db), _: User = Depends(_notification_admin)):
    cfg = notification_service._smtp_config()
    if cfg is None:
        raise HTTPException(
            status_code=400,
            detail="SMTP isn't fully configured yet -- set a host and at least one recipient, and enable it, then try again.",
        )
    try:
        notification_service.send_smtp(
            cfg,
            "[NetGuard] Test notification",
            "This is a test email from NetGuard's Integrations page. "
            "If you're reading this, email alerting is configured correctly.",
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced directly to the admin testing the connection
        return NotificationTestResult(success=False, detail=str(exc)[:500])
    return NotificationTestResult(success=True, detail=f"Test email sent to {', '.join(cfg.recipients)}.")
