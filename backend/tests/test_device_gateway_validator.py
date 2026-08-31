"""Proves the Device Gateway's independent validation actually rejects
what it's supposed to -- forged signatures, expired jobs, replayed jobs,
cross-tenant jobs, unapproved mutating operations, and self-approved
change requests -- even when the job's other fields look well-formed.

These map directly to the "Security Testing" checklist (Section 20:
Device execution -- unauthorized job rejected, expired job rejected,
replayed job rejected, wrong tenant rejected) and to the final
acceptance criterion ("bypass required approvals" must not be possible).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.device_gateway import validator
from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceVendor
from app.models.jit_elevation import JitElevation, JitElevationStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.device_job import DeviceJobRequest, DeviceOperation, sign

SIGNING_KEY = "test-signing-key"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def tenant_and_device(db):
    tenant = Tenant(id=uuid.uuid4(), name="Acme", slug="acme")
    db.add(tenant)
    db.commit()
    device = Device(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        hostname="core-router-01",
        ip_address="10.0.0.1",
        vendor=DeviceVendor.CISCO,
    )
    db.add(device)
    db.commit()
    return tenant, device


def _make_job(tenant, device, *, operation=DeviceOperation.GET_RUNNING_CONFIG, requested_by="user-1", **overrides):
    now = datetime.now(timezone.utc)
    job = DeviceJobRequest(
        job_id=overrides.get("job_id", str(uuid.uuid4())),
        tenant_id=overrides.get("tenant_id", str(tenant.id)),
        device_id=overrides.get("device_id", str(device.id)),
        operation=operation,
        params={},
        requested_by=requested_by,
        change_request_id=overrides.get("change_request_id"),
        approval_id=overrides.get("approval_id"),
        jit_elevation_id=overrides.get("jit_elevation_id"),
        issued_at=now.isoformat(),
        expires_at=overrides.get("expires_at", (now + timedelta(minutes=2)).isoformat()),
    )
    if overrides.get("skip_sign"):
        return job
    return sign(job, overrides.get("key", SIGNING_KEY))


def test_valid_readonly_job_is_accepted(db, tenant_and_device):
    tenant, device = tenant_and_device
    job = _make_job(tenant, device)
    validated_device = validator.validate(job, db, SIGNING_KEY)
    assert validated_device.id == device.id


def test_forged_signature_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    job = _make_job(tenant, device, key="wrong-key")
    with pytest.raises(validator.JobRejected, match="signature"):
        validator.validate(job, db, SIGNING_KEY)


def test_tampered_payload_is_rejected(db, tenant_and_device):
    """Signature was computed over the original device_id -- swapping it
    after signing (e.g. an attacker intercepting/rewriting the message)
    must invalidate the signature, not silently validate against the new
    device."""
    tenant, device = tenant_and_device
    job = _make_job(tenant, device)
    job.device_id = str(uuid.uuid4())
    with pytest.raises(validator.JobRejected, match="signature"):
        validator.validate(job, db, SIGNING_KEY)


def test_expired_job_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    job = _make_job(tenant, device, expires_at=past)
    with pytest.raises(validator.JobRejected, match="expired"):
        validator.validate(job, db, SIGNING_KEY)


def test_replayed_job_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    job = _make_job(tenant, device)
    validator.validate(job, db, SIGNING_KEY)  # first execution succeeds
    with pytest.raises(validator.JobRejected, match="replay"):
        validator.validate(job, db, SIGNING_KEY)  # second time: rejected


def test_wrong_tenant_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    other_tenant_id = str(uuid.uuid4())
    job = _make_job(tenant, device, tenant_id=other_tenant_id)
    with pytest.raises(validator.JobRejected, match="tenant mismatch"):
        validator.validate(job, db, SIGNING_KEY)


def test_unknown_device_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    job = _make_job(tenant, device, device_id=str(uuid.uuid4()))
    with pytest.raises(validator.JobRejected, match="not found"):
        validator.validate(job, db, SIGNING_KEY)


def test_mutating_op_without_change_request_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    job = _make_job(tenant, device, operation=DeviceOperation.DEPLOY_CONFIG)
    with pytest.raises(validator.JobRejected, match="no change_request_id"):
        validator.validate(job, db, SIGNING_KEY)


def test_mutating_op_with_unapproved_change_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ADMIN))
    db.commit()
    change = ChangeRequest(
        id=uuid.uuid4(),
        device_id=device.id,
        submitted_by=user_id,
        priority=ChangePriority.HIGH,
        description="test change",
        proposed_config="hostname test",
        status=ChangeStatus.PENDING_APPROVAL,
    )
    db.add(change)
    db.commit()
    job = _make_job(
        tenant, device, operation=DeviceOperation.DEPLOY_CONFIG, change_request_id=str(change.id)
    )
    with pytest.raises(validator.JobRejected, match="not approved"):
        validator.validate(job, db, SIGNING_KEY)


def test_self_approved_change_is_rejected(db, tenant_and_device):
    """Even if the API's own approval endpoint has a bug that lets
    someone approve their own change, the Gateway independently refuses
    to execute it -- this is the defense-in-depth check for Section 11
    (Four-Eyes Control)."""
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ADMIN))
    db.commit()
    change = ChangeRequest(
        id=uuid.uuid4(),
        device_id=device.id,
        submitted_by=user_id,
        approved_by=user_id,  # same person -- should never pass
        priority=ChangePriority.HIGH,
        description="test change",
        proposed_config="hostname test",
        status=ChangeStatus.APPROVED,
    )
    db.add(change)
    db.commit()
    job = _make_job(
        tenant, device, operation=DeviceOperation.DEPLOY_CONFIG, change_request_id=str(change.id)
    )
    with pytest.raises(validator.JobRejected, match="self-approved"):
        validator.validate(job, db, SIGNING_KEY)


def test_self_approved_rollback_change_is_accepted(db, tenant_and_device):
    """A rollback CR (is_rollback="true") is the one intentional
    exception to the self-approval check above -- see
    app.services.rollback_service.initiate_rollback, which self-approves
    an emergency rollback by design and records it explicitly in the
    audit trail. Without this carve-out, DEVICE_GATEWAY_ENABLED=true
    silently breaks every manual/emergency rollback, since rollback_service
    has always self-approved and the Gateway's DEPLOY_CONFIG/ROLLBACK_CONFIG
    jobs for a rollback carry that same CR's change_request_id."""
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ADMIN))
    db.commit()
    change = ChangeRequest(
        id=uuid.uuid4(),
        device_id=device.id,
        submitted_by=user_id,
        approved_by=user_id,  # same person -- allowed only because is_rollback="true"
        priority=ChangePriority.EMERGENCY,
        description="rollback to snapshot v3",
        proposed_config="hostname test",
        status=ChangeStatus.APPROVED,
        is_rollback="true",
    )
    db.add(change)
    db.commit()
    job = _make_job(
        tenant, device, operation=DeviceOperation.ROLLBACK_CONFIG, change_request_id=str(change.id)
    )
    # Should not raise.
    validator.validate(job, db, SIGNING_KEY)


def test_mutating_op_with_properly_approved_change_is_accepted(db, tenant_and_device):
    tenant, device = tenant_and_device
    requester_id, approver_id = uuid.uuid4(), uuid.uuid4()
    db.add_all([
        User(id=requester_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ADMIN),
        User(id=approver_id, email="admin@acme.test", full_name="Admin", hashed_password="x", role=UserRole.NETWORK_ADMIN),
    ])
    db.commit()
    change = ChangeRequest(
        id=uuid.uuid4(),
        device_id=device.id,
        submitted_by=requester_id,
        approved_by=approver_id,
        priority=ChangePriority.HIGH,
        description="test change",
        proposed_config="hostname test",
        status=ChangeStatus.APPROVED,
    )
    db.add(change)
    db.commit()
    job = _make_job(
        tenant, device, operation=DeviceOperation.DEPLOY_CONFIG, change_request_id=str(change.id)
    )
    validated_device = validator.validate(job, db, SIGNING_KEY)
    assert validated_device.id == device.id


def _make_elevation(db, *, user_id, device_id=None, scoped_operation=None, status=JitElevationStatus.ACTIVE, expires_at=None):
    elevation = JitElevation(
        id=uuid.uuid4(),
        user_id=user_id,
        elevated_role="network_admin",
        reason="test",
        requested_by=user_id,
        requested_duration_minutes=15,
        status=status,
        device_id=device_id,
        scoped_operation=scoped_operation,
        activated_at=datetime.now(timezone.utc) if status == JitElevationStatus.ACTIVE else None,
        expires_at=expires_at if expires_at is not None else datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db.add(elevation)
    db.commit()
    return elevation


def test_active_unscoped_jit_elevation_is_accepted(db, tenant_and_device):
    """Regression test for a real bug: this check previously compared
    against JitElevationStatus.APPROVED, a status that doesn't exist on
    the enum, which meant every job carrying a jit_elevation_id would
    raise AttributeError instead of validating."""
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id)
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id))
    validated_device = validator.validate(job, db, SIGNING_KEY)
    assert validated_device.id == device.id


def test_jit_elevation_scoped_to_other_device_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    other_device_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.add(Device(id=other_device_id, tenant_id=tenant.id, hostname="core-router-02", ip_address="10.0.0.2", vendor=DeviceVendor.CISCO))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id, device_id=other_device_id)
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id))
    with pytest.raises(validator.JobRejected, match="is scoped to device"):
        validator.validate(job, db, SIGNING_KEY)


def test_jit_elevation_scoped_to_matching_device_is_accepted(db, tenant_and_device):
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id, device_id=device.id, scoped_operation=DeviceOperation.GET_RUNNING_CONFIG.value)
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id))
    validated_device = validator.validate(job, db, SIGNING_KEY)
    assert validated_device.id == device.id


def test_jit_elevation_scoped_to_other_operation_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id, device_id=device.id, scoped_operation=DeviceOperation.GET_STARTUP_CONFIG.value)
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id), operation=DeviceOperation.GET_RUNNING_CONFIG)
    with pytest.raises(validator.JobRejected, match="is scoped to operation"):
        validator.validate(job, db, SIGNING_KEY)


def test_expired_jit_elevation_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id))
    with pytest.raises(validator.JobRejected, match="expired"):
        validator.validate(job, db, SIGNING_KEY)


def test_non_active_jit_elevation_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email="eng@acme.test", full_name="Eng", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=user_id, status=JitElevationStatus.PENDING)
    job = _make_job(tenant, device, requested_by=str(user_id), jit_elevation_id=str(elevation.id))
    with pytest.raises(validator.JobRejected, match="not active"):
        validator.validate(job, db, SIGNING_KEY)


def test_jit_elevation_belonging_to_other_user_is_rejected(db, tenant_and_device):
    tenant, device = tenant_and_device
    owner_id, requester_id = uuid.uuid4(), uuid.uuid4()
    db.add(User(id=owner_id, email="owner@acme.test", full_name="Owner", hashed_password="x", role=UserRole.NETWORK_ENGINEER))
    db.commit()
    elevation = _make_elevation(db, user_id=owner_id)
    job = _make_job(tenant, device, requested_by=str(requester_id), jit_elevation_id=str(elevation.id))
    with pytest.raises(validator.JobRejected, match="does not belong"):
        validator.validate(job, db, SIGNING_KEY)
