"""Tests for app.services.notification_service._smtp_config -- DB-backed
notification settings (Integrations page) should take priority over the
SMTP_* env vars, and email should be skipped cleanly when neither path
is configured.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.notification_settings import SETTINGS_ROW_ID, NotificationSettings
from app.services import notification_service


@pytest.fixture()
def db_session(monkeypatch):
    # StaticPool: notification_service._smtp_config() opens its own new
    # session via SessionLocal() -- a plain in-memory sqlite db is
    # per-connection, so without a shared pool that second session would
    # see an empty (freshly created) database, not the row this fixture
    # just committed.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Point notification_service's internal SessionLocal() calls at this
    # same in-memory engine/session factory instead of the real DB.
    monkeypatch.setattr(notification_service, "SessionLocal", Session)

    yield session
    session.close()


def test_no_config_returns_none(db_session):
    with patch.object(notification_service.settings, "SMTP_HOST", None), \
         patch.object(notification_service.settings, "NOTIFY_EMAIL_RECIPIENTS", None):
        assert notification_service._smtp_config() is None


def test_env_fallback_used_when_no_db_row(db_session):
    with patch.object(notification_service.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(notification_service.settings, "NOTIFY_EMAIL_RECIPIENTS", "ops@example.com"), \
         patch.object(notification_service.settings, "SMTP_PORT", 587):
        cfg = notification_service._smtp_config()
        assert cfg is not None
        assert cfg.host == "smtp.example.com"
        assert cfg.recipients == ["ops@example.com"]


def test_db_settings_take_priority_over_env(db_session):
    row = NotificationSettings(
        id=SETTINGS_ROW_ID,
        smtp_enabled=True,
        smtp_host="db-smtp.example.com",
        smtp_port=2525,
        recipients="noc@example.com, oncall@example.com",
        smtp_from_email="alerts@example.com",
    )
    db_session.add(row)
    db_session.commit()

    with patch.object(notification_service.settings, "SMTP_HOST", "env-smtp.example.com"), \
         patch.object(notification_service.settings, "NOTIFY_EMAIL_RECIPIENTS", "envonly@example.com"):
        cfg = notification_service._smtp_config()
        assert cfg is not None
        assert cfg.host == "db-smtp.example.com"
        assert cfg.port == 2525
        assert cfg.recipients == ["noc@example.com", "oncall@example.com"]


def test_db_row_present_but_disabled_falls_back_to_env(db_session):
    row = NotificationSettings(id=SETTINGS_ROW_ID, smtp_enabled=False, smtp_host="db-smtp.example.com", recipients="noc@example.com")
    db_session.add(row)
    db_session.commit()

    with patch.object(notification_service.settings, "SMTP_HOST", "env-smtp.example.com"), \
         patch.object(notification_service.settings, "NOTIFY_EMAIL_RECIPIENTS", "envonly@example.com"):
        cfg = notification_service._smtp_config()
        assert cfg is not None
        assert cfg.host == "env-smtp.example.com"


def test_db_row_enabled_but_no_recipients_is_none(db_session):
    row = NotificationSettings(id=SETTINGS_ROW_ID, smtp_enabled=True, smtp_host="db-smtp.example.com", recipients=None)
    db_session.add(row)
    db_session.commit()

    with patch.object(notification_service.settings, "SMTP_HOST", None), \
         patch.object(notification_service.settings, "NOTIFY_EMAIL_RECIPIENTS", None):
        assert notification_service._smtp_config() is None
