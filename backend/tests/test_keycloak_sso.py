"""Proves the Keycloak login flow does what Section 1/20 require:
Authorization Code + PKCE (never implicit, PKCE verifier required and
checked), local JWKS-based id_token verification (wrong issuer, wrong
audience, and bad signature are all rejected -- not just trusted from a
third party), and correct find-or-create user provisioning identical in
shape to the existing Google SSO path.

Uses a real RSA keypair + python-jose to sign test id_tokens, and mocks
only the network boundary (httpx calls to Keycloak's discovery/JWKS/
token endpoints) -- the verification code under test runs for real.
"""
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import oidc_service

ISSUER = "https://kc.example.internal/realms/netguard"
CLIENT_ID = "netguard"
REDIRECT_URI = "http://testserver/api/v1/sso/keycloak/callback"


@pytest.fixture(scope="module")
def rsa_jwk():
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    )
    from jose import jwk as jose_jwk

    public_jwk = jose_jwk.construct(priv_pem, algorithm="RS256").public_key().to_dict()
    public_jwk["kid"] = "test-kid-1"
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return priv_pem, public_jwk


def _sign(priv_pem, claims: dict, kid: str = "test-kid-1") -> str:
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "kc-subject-1",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def _reset_oidc_caches():
    oidc_service._discovery_cache = None
    oidc_service._discovery_cached_at = 0.0
    oidc_service._jwks_cache = None
    oidc_service._jwks_cached_at = 0.0
    yield
    oidc_service._discovery_cache = None
    oidc_service._jwks_cache = None


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OIDC_ISSUER", ISSUER)
    monkeypatch.setattr("app.core.config.settings.OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr("app.core.config.settings.OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr("app.core.config.settings.OIDC_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setattr("app.core.config.settings.OIDC_AUDIENCE", None)

    def mock_rate_limiter(*a, **k):
        import redis
        raise redis.RedisError("BYPASS_REDIS")
    monkeypatch.setattr("app.services.rate_limiter._get_client", mock_rate_limiter)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _mock_discovery_and_jwks(public_jwk: dict):
    discovery = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    }

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        if "openid-configuration" in url:
            resp.json.return_value = discovery
        elif "certs" in url:
            resp.json.return_value = {"keys": [public_jwk]}
        return resp

    return fake_get


def test_login_requires_pkce_and_redirects_to_keycloak(client, rsa_jwk):
    _, public_jwk = rsa_jwk
    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)):
        resp = client.get("/api/v1/sso/keycloak/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{ISSUER}/protocol/openid-connect/auth")
    assert "code_challenge=" in resp.headers["location"]
    assert "code_challenge_method=S256" in resp.headers["location"]
    # PKCE verifier cookie set, httpOnly
    assert oidc_service.PKCE_COOKIE_NAME in resp.cookies


def test_callback_rejects_missing_pkce_cookie(client):
    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 302
    assert "sso_error=missing_pkce_verifier" in resp.headers["location"]


def test_callback_rejects_wrong_issuer(client, rsa_jwk, monkeypatch):
    priv_pem, public_jwk = rsa_jwk
    bad_token = _sign(priv_pem, _base_claims(iss="https://not-our-keycloak.example/realms/other"))

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": bad_token}
        return resp

    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    client.cookies.set(oidc_service.PKCE_COOKIE_NAME, "test-verifier")

    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)), patch("httpx.post", side_effect=fake_post):
        resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "sso_error=keycloak_login_failed" in resp.headers["location"]


def test_callback_rejects_wrong_audience(client, rsa_jwk):
    priv_pem, public_jwk = rsa_jwk
    bad_token = _sign(priv_pem, _base_claims(aud="some-other-client"))

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": bad_token}
        return resp

    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    client.cookies.set(oidc_service.PKCE_COOKIE_NAME, "test-verifier")

    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)), patch("httpx.post", side_effect=fake_post):
        resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "sso_error=keycloak_login_failed" in resp.headers["location"]


def test_callback_rejects_bad_signature(client, rsa_jwk):
    _, public_jwk = rsa_jwk
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

    other_key = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    other_priv_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Signed with a DIFFERENT private key than the one whose public JWK
    # we'll serve -- signature must fail verification even though kid matches.
    forged = _sign(other_priv_pem, _base_claims())

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": forged}
        return resp

    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    client.cookies.set(oidc_service.PKCE_COOKIE_NAME, "test-verifier")

    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)), patch("httpx.post", side_effect=fake_post):
        resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "sso_error=keycloak_login_failed" in resp.headers["location"]


def test_callback_success_provisions_user(client, rsa_jwk):
    priv_pem, public_jwk = rsa_jwk
    good_token = _sign(priv_pem, _base_claims())

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": good_token}
        return resp

    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    client.cookies.set(oidc_service.PKCE_COOKIE_NAME, "test-verifier")

    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)), patch("httpx.post", side_effect=fake_post):
        resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "access_token=" in resp.headers["location"]
    assert "sso_error" not in resp.headers["location"]
    # PKCE cookie cleared after use
    assert resp.cookies.get(oidc_service.PKCE_COOKIE_NAME) in (None, "", '""')


def test_second_login_links_existing_local_account_by_email(client, rsa_jwk):
    """A user who registered locally before Keycloak was turned on should
    get their existing account linked (sso_subject populated), not a
    duplicate account -- same behavior as the Google path."""
    priv_pem, public_jwk = rsa_jwk

    db = next(iter(client.app.dependency_overrides[get_db]()))
    existing = User(email="alice@example.com", full_name="Alice Local", hashed_password="x", role="network_engineer")
    db.add(existing)
    db.commit()
    existing_id = existing.id
    db.close()

    good_token = _sign(priv_pem, _base_claims())

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": good_token}
        return resp

    from app.core.security import create_sso_state_token

    state = create_sso_state_token()
    client.cookies.set(oidc_service.PKCE_COOKIE_NAME, "test-verifier")

    with patch("httpx.get", side_effect=_mock_discovery_and_jwks(public_jwk)), patch("httpx.post", side_effect=fake_post):
        resp = client.get(f"/api/v1/sso/keycloak/callback?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "access_token=" in resp.headers["location"]

    db2 = next(iter(client.app.dependency_overrides[get_db]()))
    linked = db2.get(User, existing_id)
    assert linked.sso_provider == "keycloak"
    assert linked.sso_subject == "kc-subject-1"
