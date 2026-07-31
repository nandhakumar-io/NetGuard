"""Deployment Pipeline Orchestrator.

Ties together the individual engines (snapshot, deployment, health monitor,
rollback, notifications, audit) into the end-to-end workflow described in
SRS section 3 (Proposed Solution & Workflow) and section 6.8 (Self-Healing
Rollback Engine):

    Approved -> Snapshot -> Deploy -> Health Monitor -> Success | Rollback -> Notify

Kept synchronous for the prototype so it can run inline behind an API call;
in production this would be dispatched as a Celery task.
"""
import uuid

from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.deployment import Deployment, DeploymentStatus, HealthCheckResult
from app.models.snapshot import ConfigSnapshot
from app.services import (
    audit_service,
    deployment_engine,
    health_monitor,
    notification_service,
    snapshot_service,
)

DEVICE_TYPE_MAP = {
    "cisco": "cisco_ios",
    "juniper": "juniper_junos",
    "arista": "arista_eos",
    "linux": "linux",
}


def run_deployment_pipeline(db: Session, cr: ChangeRequest, actor_email: str) -> ChangeRequest:
    """Executes the full deploy -> monitor -> (rollback) pipeline for an
    already-approved change request, persisting a Deployment record, a
    ConfigSnapshot, HealthCheckResult rows, audit log entries, and
    notifications along the way.
    """
    device: Device | None = db.get(Device, cr.device_id)
    if device is None:
        raise ValueError("Device not found for change request")

    netmiko_type = DEVICE_TYPE_MAP.get(device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios")

    # --- 1. Automatic Configuration Snapshot (FR-7) ---
    version = str(int(uuid.uuid4().int % 1_000_000))
    snapshot_payload = snapshot_service.build_snapshot_payload(
        running_config=cr.current_config or "! (no prior running-config on file)",
        startup_config=cr.current_config,
        version=version,
    )
    snapshot = ConfigSnapshot(device_id=device.id, change_request_id=cr.id, **snapshot_payload)
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    audit_service.record_event(
        db, actor="system", action="Snapshot", result="Completed",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=f"version={snapshot.version} checksum={snapshot.checksum[:12]}...",
    )

    deployment = Deployment(
        change_request_id=cr.id, device_id=device.id, snapshot_id=snapshot.id,
        status=DeploymentStatus.IN_PROGRESS, protocol="ssh",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    cr.status = ChangeStatus.DEPLOYING
    db.commit()

    # --- 2. Configuration Deployment (FR-8) ---
    config_commands = [line for line in cr.proposed_config.splitlines() if line.strip()]
    deploy_result = deployment_engine.deploy_config(
        hostname=device.hostname,
        ip_address=device.ip_address,
        device_type=netmiko_type,
        username=device.ssh_username or "admin",
        password="",  # prototype: real credentials come from a secret store, not stored in DB
        config_commands=config_commands,
    )

    if not deploy_result.success:
        deployment.status = DeploymentStatus.FAILED
        deployment.error_message = deploy_result.error
        cr.status = ChangeStatus.FAILED
        db.commit()
        audit_service.record_event(
            db, actor="system", action="Deployment", result="Failed",
            device_hostname=device.hostname, change_request_id=cr.id, detail=deploy_result.error,
        )
        notification_service.notify(
            "Deployment Failed", f"{device.hostname}: {deploy_result.error}", severity="critical",
        )
        return cr

    audit_service.record_event(
        db, actor="system", action="Deployment", result="Success",
        device_hostname=device.hostname, change_request_id=cr.id,
    )

    # --- 3. Real-Time Health Monitoring (FR-9) ---
    cr.status = ChangeStatus.MONITORING
    db.commit()

    outcomes = health_monitor.run_health_suite(device.ip_address)
    for outcome in outcomes:
        db.add(HealthCheckResult(
            deployment_id=deployment.id,
            category=outcome.category,
            check_name=outcome.check_name,
            passed="true" if outcome.passed else "false",
            detail=outcome.detail,
        ))
    db.commit()

    healthy = health_monitor.suite_passed(outcomes)

    if healthy:
        deployment.status = DeploymentStatus.SUCCEEDED
        cr.status = ChangeStatus.SUCCESS
        db.commit()
        audit_service.record_event(
            db, actor="system", action="Health Check", result="Passed",
            device_hostname=device.hostname, change_request_id=cr.id,
        )
        notification_service.notify(
            "Deployment Succeeded", f"{device.hostname}: change deployed and healthy.", severity="info",
        )
        return cr

    # --- 4. Self-Healing Rollback Engine (FR-10) ---
    audit_service.record_event(
        db, actor="system", action="Health Check", result="Failed",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail="; ".join(o.detail for o in outcomes if not o.passed),
    )

    restore_commands = (snapshot_service.decrypt_config(snapshot.running_config_encrypted)).splitlines()
    rollback_result = deployment_engine.rollback_config(
        hostname=device.hostname,
        ip_address=device.ip_address,
        device_type=netmiko_type,
        username=device.ssh_username or "admin",
        password="",
        restore_commands=[line for line in restore_commands if line.strip()],
    )

    deployment.status = DeploymentStatus.ROLLED_BACK if rollback_result.success else DeploymentStatus.FAILED
    deployment.error_message = rollback_result.error
    cr.status = ChangeStatus.ROLLED_BACK if rollback_result.success else ChangeStatus.FAILED
    db.commit()

    audit_service.record_event(
        db, actor="system", action="Rollback",
        result="Completed" if rollback_result.success else "Failed",
        device_hostname=device.hostname, change_request_id=cr.id, detail=rollback_result.error,
    )
    notification_service.notify(
        "Automatic Rollback Triggered",
        f"{device.hostname}: health checks failed after deployment. Rollback "
        f"{'succeeded' if rollback_result.success else 'FAILED — manual intervention required'}.",
        severity="critical",
    )
    return cr
