from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import create_sso_state_token
from app.main import app
from app.models.user import User, UserRole
from app.services import rate_limiter


@pytest.fixture()
def client(monkeypatch):
    def mock_get_client(*args, **kwargs):
        import redis
        raise redis.RedisError("BYPASS_REDIS")
    monkeypatch.setattr("app.services.rate_limiter._get_client", mock_get_client)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    rate_limiter._fallback_attempts.clear()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _configure_google(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr("app.core.config.settings.GOOGLE_REDIRECT_URI", "http://testserver/api/v1/sso/google/callback")


def test_providers_reports_disabled_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr("app.core.config.settings.OIDC_ISSUER", None)
    resp = client.get("/api/v1/sso/providers")
    assert resp.status_code == 200
    assert resp.json() == {"google": False, "keycloak": False, "local": True}


def test_providers_reports_enabled_when_configured(client, monkeypatch):
    _configure_google(monkeypatch)
    resp = client.get("/api/v1/sso/providers")
    assert resp.json()["google"] is True


def test_login_redirects_to_google_when_configured(client, monkeypatch):
    _configure_google(monkeypatch)
    resp = client.get("/api/v1/sso/google/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "state=" in resp.headers["location"]


def test_login_redirects_to_login_page_when_not_configured(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_ID", None)
    resp = client.get("/api/v1/sso/google/login", follow_redirects=False)
    assert resp.status_code == 302
    assert "sso_error=sso_not_configured" in resp.headers["location"]


def test_callback_rejects_missing_state(client, monkeypatch):
    _configure_google(monkeypatch)
    resp = client.get("/api/v1/sso/google/callback", params={"code": "abc"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "sso_error=missing_code_or_state" in resp.headers["location"]


def test_callback_rejects_tampered_state(client, monkeypatch):
    _configure_google(monkeypatch)
    resp = client.get(
        "/api/v1/sso/google/callback",
        params={"code": "abc", "state": "not-a-real-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "sso_error=invalid_or_expired_state" in resp.headers["location"]


def test_callback_surfaces_google_error_param(client, monkeypatch):
    _configure_google(monkeypatch)
    resp = client.get(
        "/api/v1/sso/google/callback",
        params={"error": "access_denied", "state": create_sso_state_token()},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "sso_error=access_denied" in resp.headers["location"]


def _fake_claims(email="newuser@acme.com", sub="google-sub-123", hd=None):
    claims = {
        "sub": sub,
        "email": email,
        "email_verified": "true",
        "aud": "test-client-id",
        "name": "New User",
    }
    if hd:
        claims["hd"] = hd
    return claims


def test_callback_provisions_new_user_and_issues_token(client, monkeypatch):
    _configure_google(monkeypatch)
    state = create_sso_state_token()

    with patch("app.services.sso_service.exchange_code_for_claims", return_value=_fake_claims()):
        resp = client.get(
            "/api/v1/sso/google/callback",
            params={"code": "valid-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "access_token=" in resp.headers["location"]
    assert "netguard_refresh_token" in resp.headers.get("set-cookie", "")


def test_callback_links_existing_local_account_by_email(client, monkeypatch):
    _configure_google(monkeypatch)
    # Register a local account first.
    client.post("/api/v1/auth/register", json={
        "email": "existing@acme.com", "full_name": "Existing User",
        "password": "correct-horse-1", "role": "network_engineer",
    })

    state = create_sso_state_token()
    with patch(
        "app.services.sso_service.exchange_code_for_claims",
        return_value=_fake_claims(email="existing@acme.com", sub="google-sub-999"),
    ):
        resp = client.get(
            "/api/v1/sso/google/callback",
            params={"code": "valid-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "access_token=" in resp.headers["location"]

    # Only one user should exist for that email -- linked, not duplicated.
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        matches = db.query(User).filter(User.email == "existing@acme.com").all()
        assert len(matches) == 1
        assert matches[0].sso_provider == "google"
        assert matches[0].sso_subject == "google-sub-999"
        assert matches[0].role == UserRole.NETWORK_ENGINEER
    finally:
        db_gen.close()


def test_callback_rejects_unverified_email(client, monkeypatch):
    _configure_google(monkeypatch)
    state = create_sso_state_token()
    claims = _fake_claims()
    claims["email_verified"] = "false"

    with patch("app.services.sso_service.exchange_code_for_claims", side_effect=None):
        from app.services import sso_service as svc

        def _raise(*a, **k):
            raise svc.SsoLoginError("Google account email is not verified")

        with patch("app.services.sso_service.exchange_code_for_claims", side_effect=_raise):
            resp = client.get(
                "/api/v1/sso/google/callback",
                params={"code": "valid-code", "state": state},
                follow_redirects=False,
            )

    assert resp.status_code == 302
    assert "sso_error=google_login_failed" in resp.headers["location"]
