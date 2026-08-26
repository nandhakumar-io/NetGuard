import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app


@pytest.fixture()
def client(monkeypatch):
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
    # Dummy user dependency
    from app.core.deps import get_current_user
    from app.models.user import User

    def override_get_current_user():
        return User(id=uuid.uuid4(), email="test@test.com", role="network_admin", is_msp_staff=True)

    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_push_sub_save(client):
    # test browser
    payload = {
        "label": "Test Browser",
        "provider": "browser",
        "endpoint": "https://fcm.googleapis.com/fcm/send/foo",
        "p256dh": "dummy-p256dh",
        "auth": "dummy-auth",
        "include_non_critical": False,
    }
    r = client.post("/api/v1/push-subscriptions", json=payload)
    print("Browser save:", r.status_code, r.text)
    assert r.status_code == 201, r.text

    # test ntfy
    payload_ntfy = {
        "label": "Test Ntfy",
        "provider": "ntfy",
        "target": "https://ntfy.sh/test",
        "include_non_critical": False,
    }
    r2 = client.post("/api/v1/push-subscriptions", json=payload_ntfy)
    print("Ntfy save:", r2.status_code, r2.text)
    assert r2.status_code == 201, r2.text

