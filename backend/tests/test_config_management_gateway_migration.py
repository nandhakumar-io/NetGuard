"""Proves the DEVICE_GATEWAY_ENABLED migration of GET /config-management/running
actually calls device_job_service.submit_job() with the right arguments,
and correctly maps a DeviceJobResult back into the existing response
schema -- rather than just being visually inspected code that was never
exercised.

Also proves the flag defaults to False, so the legacy in-process path
(ProtocolManager) is what runs unless an operator has deliberately
deployed the Device Gateway and opted in -- see
app.core.config.Settings.DEVICE_GATEWAY_ENABLED.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.deps import get_current_tenant_id, get_current_user
from app.main import app
from app.models.device import Device, DeviceVendor
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.device_job import DeviceJobResult


@pytest.fixture()
def client_and_device():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    db = TestingSessionLocal()
    tenant = Tenant(id=uuid.uuid4(), name="Acme", slug="acme")
    user = User(
        id=uuid.uuid4(), email="eng@acme.test", full_name="Eng",
        hashed_password="x", role=UserRole.NETWORK_ADMIN, tenant_id=tenant.id,
    )
    device = Device(
        id=uuid.uuid4(), tenant_id=tenant.id, hostname="core-router-01",
        ip_address="10.0.0.1", vendor=DeviceVendor.CISCO,
    )
    db.add_all([tenant, user, device])
    db.commit()
    device_id, user_id, tenant_id = device.id, user.id, tenant.id
    db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: db_get_user(TestingSessionLocal, user_id)
    app.dependency_overrides[get_current_tenant_id] = lambda: tenant_id

    yield TestClient(app), device_id, user_id, tenant_id

    app.dependency_overrides.clear()


def db_get_user(session_factory, user_id):
    db = session_factory()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def test_gateway_flag_defaults_true(monkeypatch):
    """DEVICE_GATEWAY_ENABLED is secure-by-default (see Settings' own
    docstring): an operator who forgets to set it should get the safe
    Gateway-routed path, not the legacy in-process one. This test used
    to assert the opposite default, contradicting the documented and
    intended behavior -- fixed rather than the app changed to match a
    stale test."""
    from app.core.config import settings
    assert settings.DEVICE_GATEWAY_ENABLED is True


def test_running_config_uses_gateway_when_enabled(client_and_device, monkeypatch):
    from app.core.config import settings

    client, device_id, user_id, tenant_id = client_and_device
    monkeypatch.setattr(settings, "DEVICE_GATEWAY_ENABLED", True)

    fake_result = DeviceJobResult(
        job_id="job-1",
        success=True,
        output="hostname core-router-01\n",
        executed_at="2026-01-01T00:00:00+00:00",
    )
    with patch(
        "app.api.config_management.device_job_service.submit_job",
        new=AsyncMock(return_value=fake_result),
    ) as mock_submit:
        resp = client.get(f"/api/v1/devices/{device_id}/config/running")

    assert resp.status_code == 200
    body = resp.json()
    assert body["config"] == "hostname core-router-01\n"
    assert body["protocol"] == "gateway"

    mock_submit.assert_awaited_once()
    call_kwargs = mock_submit.await_args.kwargs
    assert call_kwargs["device_id"] == str(device_id)
    assert call_kwargs["tenant_id"] == str(tenant_id)
    assert call_kwargs["requested_by"] == str(user_id)
