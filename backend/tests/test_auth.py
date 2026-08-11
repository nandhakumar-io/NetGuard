import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.services import rate_limiter


@pytest.fixture()
def client():
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

    # Ensure rate limits state is completely cleared before each test
    rate_limiter._fallback_attempts.clear()
    rate_limiter.reset_attempts("user@netguard.ai")
    rate_limiter.reset_attempts("mfa:user@netguard.ai")

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _register(client, email="user@netguard.ai", role="network_admin"):
    r = client.post("/api/v1/auth/register", json={
        "email": email, "full_name": "Test User", "password": "correct-horse-1", "role": role,
    })
    assert r.status_code == 201
    return r.json()["access_token"]


def test_login_without_mfa_returns_token_pair(client):
    _register(client)
    r = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body and "refresh_token" in body


def test_mfa_enrollment_and_challenge_flow(client):
    token = _register(client)
    h = {"Authorization": f"Bearer {token}"}

    setup = client.post("/api/v1/auth/mfa/setup", headers=h)
    assert setup.status_code == 200
    secret = setup.json()["secret"]

    code = pyotp.TOTP(secret).now()
    enable = client.post("/api/v1/auth/mfa/enable", json={"code": code}, headers=h)
    assert enable.status_code == 200
    assert enable.json()["mfa_enabled"] is True

    # Login now requires MFA
    login = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    assert login.status_code == 200
    assert login.json()["mfa_required"] is True
    mfa_token = login.json()["mfa_token"]

    # Wrong code rejected
    bad = client.post("/api/v1/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})
    assert bad.status_code == 401

    # Correct code issues a real token pair
    good_code = pyotp.TOTP(secret).now()
    verify = client.post("/api/v1/auth/mfa/verify", json={"mfa_token": mfa_token, "code": good_code})
    assert verify.status_code == 200
    assert "access_token" in verify.json() and "refresh_token" in verify.json()


def test_mfa_challenge_token_cannot_be_used_as_access_token(client):
    token = _register(client)
    h = {"Authorization": f"Bearer {token}"}
    setup = client.post("/api/v1/auth/mfa/setup", headers=h)
    secret = setup.json()["secret"]
    code = pyotp.TOTP(secret).now()
    client.post("/api/v1/auth/mfa/enable", json={"code": code}, headers=h)

    login = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    mfa_token = login.json()["mfa_token"]

    # An mfa_challenge token must be rejected by protected endpoints
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {mfa_token}"})
    assert r.status_code == 401


def test_refresh_token_rotation(client):
    _register(client)
    login = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    refresh_token = login.json()["refresh_token"]

    r1 = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r1.status_code == 200
    new_refresh = r1.json()["refresh_token"]
    assert new_refresh != refresh_token

    # Old refresh token is now revoked -- reuse must fail
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 401


def test_logout_revokes_refresh_token(client):
    _register(client)
    login = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    refresh_token = login.json()["refresh_token"]

    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r.status_code == 401


def test_login_lockout_after_max_failed_attempts(client):
    _register(client)
    for _ in range(5):
        r = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "wrong-password"})
        assert r.status_code == 401

    locked = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "wrong-password"})
    assert locked.status_code == 429

    # Even the CORRECT password is now blocked until the lockout window passes
    still_locked = client.post("/api/v1/auth/login", json={"email": "user@netguard.ai", "password": "correct-horse-1"})
    assert still_locked.status_code == 429
