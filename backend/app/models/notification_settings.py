"""DB-configurable notification channel settings, editable from the
Integrations page instead of only via environment variables at deploy
time. Currently covers SMTP email -- Slack/Teams/Telegram already have a
DB-backed equivalent (app.models.webhook.WebhookEndpoint) managed from
Alert Center's Webhooks tab, so this model deliberately doesn't
duplicate those.

Singleton table: exactly one row (id is fixed, see SETTINGS_ROW_ID),
same pattern as any other single-tenant "global settings" record. When
present, its fields take priority over the SMTP_* env vars in
app.core.config -- see app.services.notification_service._smtp_config --
so an operator can configure/change SMTP from the UI without a
redeploy, while env vars remain a valid fallback for infra that prefers
config-as-code.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base

# Fixed id for the single settings row -- there is exactly one, ever.
# Deliberately not a low/all-zero-looking value like
# 00000000-0000-0000-0000-000000000001: on SQLite (used in this
# project's test suite), a UUID column falls back to a generic type
# whose bind/result processing can get tripped up by NUMERIC column
# affinity for values that look "too much like an integer", silently
# corrupting round-trips. A properly random-looking UUID sidesteps that
# entirely and is what every other singleton/fixed-id row in this
# codebase already uses.
SETTINGS_ROW_ID = uuid.UUID("a1b2c3d4-e5f6-4a1b-8c3d-9e0f1a2b3c4d")


class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: SETTINGS_ROW_ID)

    smtp_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=False, default=587, server_default="587")
    smtp_username = Column(String, nullable=True)
    # Encrypted at rest via app.core.crypto, same as Device SSH/SNMP
    # secrets -- never store SMTP auth in plaintext.
    smtp_password_encrypted = Column(String, nullable=True)
    smtp_from_email = Column(String, nullable=True)
    smtp_use_tls = Column(Boolean, nullable=False, default=True, server_default="true")
    # Comma-separated, same format as the NOTIFY_EMAIL_RECIPIENTS env var
    # it can override.
    recipients = Column(String, nullable=True)

    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
