"""Deployment Pipeline Orchestrator.

Ties together the individual engines (snapshot, deployment, health monitor,
rollback, notifications, audit) into the end-to-end workflow described in
SRS section 3 (Proposed Solution & Workflow) and section 6.8 (Self-Healing
Rollback Engine):

    Approved -> Snapshot -> Deploy -> Health Monitor -> Success | Rollback -> Notify

Two entry points:

  - `run_deployment_for_device`: the unit of work for ONE device. This is
    what gets wrapped in a Celery task so multiple devices on the same
    change request can run concurrently (SRS 6.6 multi-device / parallel
    deployment) instead of one-at-a-time.
  - `aggregate_change_request_status`: rolls the per-device Deployment
    outcomes back up into a single ChangeStatus on the ChangeRequest once
    every device has finished.

Previously this module ran synchronously inline behind the approve API
call and handled exactly one device (`cr.device_id`). It's now designed to
be dispatched as Celery tasks (see app.tasks) -- see `run_deployment_pipeline`
at the bottom, kept as a thin synchronous entry point for tests/CLI use.
"""
import uuid

from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.deployment import Deployment, DeploymentStatus, HealthCheckResult
from app.models.snapshot import ConfigSnapshot
from app.services import (
    audit_service,
    credential_service,
    deployment_engine,
    event_bus,
    health_monitor,
    notification_service,
    protocol_manager,
    snapshot_service,
)
from app.models.deployment import DeploymentLog

DEVICE_TYPE_MAP = {
    "cisco": "cisco_ios",
    "juniper": "juniper_junos",
    "arista": "arista_eos",
    "linux": "linux",
}


def target_device_ids(cr: ChangeRequest) -> list[uuid.UUID]:
    """The full set of devices a change request targets: the primary
    `device_id` plus any `additional_device_ids` (SRS 6.6 multi-device
    deployment), de-duplicated, primary first.
    """
    import json

    ids = [cr.device_id]
    if cr.additional_device_ids:
        for raw in json.loads(cr.additional_device_ids):
            extra_id = uuid.UUID(raw) if isinstance(raw, str) else raw
            if extra_id not in ids:
                ids.append(extra_id)
    return ids


def _log_deployment(db: Session, deployment_id: uuid.UUID, step: str, message: str, level: str = "INFO"):
    log = DeploymentLog(deployment_id=deployment_id, step=step, level=level, message=message)
    db.add(log)
    db.commit()
    db.refresh(log)
    
    event_bus.publish_event(
        "deployment_log",
        deployment_id=str(deployment_id),
        log={
            "id": str(log.id),
            "step": log.step,
            "level": log.level,
            "message": log.message,
            "timestamp": log.timestamp.isoformat(),
        }
    )



def run_deployment_for_device(db: Session, cr: ChangeRequest, device_id: uuid.UUID, actor_email: str) -> Deployment:
    """Executes the full deploy -> monitor -> (rollback) pipeline for ONE
    device on an already-approved change request. Persists a Deployment
    record, a ConfigSnapshot, HealthCheckResult rows, audit log entries,
    and notifications along the way. Safe to run concurrently (in separate
    Celery tasks/DB sessions) alongside other devices on the same CR.
    """
    device: Device | None = db.get(Device, device_id)
    if device is None:
        raise ValueError(f"Device {device_id} not found for change request {cr.id}")

    netmiko_type = DEVICE_TYPE_MAP.get(
        device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios"
    )

    # --- Real credential retrieval (was: hardcoded empty password) ---
    # Resolved up front so a missing/misconfigured credential fails loudly
    # before we ever touch the snapshot or attempt a connection, and is
    # recorded in the audit trail like any other pre-flight failure.
    try:
        ssh_password = credential_service.get_ssh_password(device)
    except credential_service.CredentialNotFoundError as exc:
        deployment = Deployment(
            change_request_id=cr.id, device_id=device.id,
            status=DeploymentStatus.FAILED, protocol="ssh", error_message=str(exc),
        )
        db.add(deployment)
        db.commit()
        db.refresh(deployment)
        _log_deployment(db, deployment.id, "PRE-FLIGHT", f"Failed to retrieve credentials: {exc}", "ERROR")
        audit_service.record_event(
            db, actor="system", action="Credential Retrieval", result="Failed",
            device_hostname=device.hostname, change_request_id=cr.id, detail=str(exc),
        )
        notification_service.notify(
            "Deployment Failed", f"{device.hostname}: {exc}", severity="critical",
            device_hostname=device.hostname, change_request_id=cr.id, deployment_id=deployment.id,
        )
        event_bus.publish_event("deployment_status_changed", status=deployment.status.value, device=device.hostname)
        return deployment

    # We have a valid device, proceed to create the Deployment early to attach logs
    deployment = Deployment(
        change_request_id=cr.id, device_id=device.id,
        status=DeploymentStatus.IN_PROGRESS, protocol="ssh",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    event_bus.publish_event("deployment_status_changed", status=deployment.status.value, device=device.hostname)
    
    _log_deployment(db, deployment.id, "PRE-FLIGHT", f"Starting deployment pipeline for CR {str(cr.id)[:8]} on {device.hostname}")

    # --- 1. Automatic Configuration Snapshot (FR-7) ---
    _log_deployment(db, deployment.id, "SNAPSHOT", "Generating configuration snapshot...")
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

    deployment.snapshot_id = snapshot.id
    db.commit()

    _log_deployment(db, deployment.id, "SNAPSHOT", f"Snapshot {snapshot.version} completed securely (checksum: {snapshot.checksum[:12]}...)")
    audit_service.record_event(
        db, actor="system", action="Snapshot", result="Completed",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=f"version={snapshot.version} checksum={snapshot.checksum[:12]}...",
    )

    # --- 2. Configuration Deployment (FR-8) via ProtocolManager ---
    _log_deployment(db, deployment.id, "DEPLOY", "Deploying requested configuration lines...")
    pm = protocol_manager.ProtocolManager(db, device, operator=actor_email)
    
    # We pass the proposed_config to deploy_config
    deploy_result = pm.deploy_config(cr.proposed_config)
    deployment.protocol = deploy_result.protocol.value if hasattr(deploy_result.protocol, 'value') else deploy_result.protocol

    if not deploy_result.success:
        deployment.status = DeploymentStatus.FAILED
        deployment.error_message = deploy_result.error
        db.commit()
        msg = f"Deployment failed over {deploy_result.protocol}: {deploy_result.error}"
        _log_deployment(db, deployment.id, "DEPLOY", msg, "ERROR")
        notification_service.notify(
            "Deployment Failed", f"{device.hostname}: {deploy_result.error}", severity="critical",
            device_hostname=device.hostname, change_request_id=cr.id, deployment_id=deployment.id,
        )
        event_bus.publish_event("deployment_status_changed", status=deployment.status.value, device=device.hostname)
        return deployment

    _log_deployment(db, deployment.id, "DEPLOY", f"Deployment succeeded swiftly over {deploy_result.protocol} ({deploy_result.execution_time_ms:.0f}ms).")

    # --- 3. Real-Time Health Monitoring (FR-9) ---
    # Actually polls the full suite (BGP/OSPF adjacency, DNS/DHCP/HTTP/VPN,
    # packet loss & latency, in addition to ping) repeatedly across a
    # configurable monitoring window (SRS 6.7) rather than checking once --
    # see health_monitor.run_monitoring_window for why a single check right
    # after deploy isn't enough. Stops early on the first failing round.
    _log_deployment(db, deployment.id, "VERIFY", "Initiating health monitoring sweeps across all vectors...")
    
    monitoring = health_monitor.run_monitoring_window(
        device.ip_address,
        netmiko_type=netmiko_type,
        username=device.ssh_username or "admin",
        password=ssh_password,
        hostname=device.hostname,
    )
    for round_ in monitoring.rounds:
        _log_deployment(db, deployment.id, "VERIFY", f"Completed verification round {round_.round_number} (t+{round_.elapsed_seconds}s). Passed: {len([o for o in round_.outcomes if o.passed])}/{len(round_.outcomes)}")
        for outcome in round_.outcomes:
            db.add(HealthCheckResult(
                deployment_id=deployment.id,
                category=outcome.category,
                check_name=outcome.check_name,
                passed="true" if outcome.passed else "false",
                detail=outcome.detail,
                poll_round=round_.round_number,
                elapsed_seconds=round_.elapsed_seconds,
            ))
    db.commit()

    healthy = monitoring.healthy
    outcomes = monitoring.rounds[-1].outcomes  # most recent round, for failure detail below

    if healthy:
        deployment.status = DeploymentStatus.SUCCEEDED
        db.commit()
        
        detail_msg = f"All {len(monitoring.rounds)} health sweep(s) completed cleanly."
        _log_deployment(db, deployment.id, "VERIFY", detail_msg)
        _log_deployment(db, deployment.id, "COMPLETE", "Deployment successfully finalized.")
        
        audit_service.record_event(
            db, actor="system", action="Health Check", result="Passed",
            device_hostname=device.hostname, change_request_id=cr.id,
            detail=(
                f"{len(monitoring.rounds)} poll round(s) over "
                f"{monitoring.window_seconds}s (every {monitoring.poll_interval_seconds}s), all passed"
            ),
        )
        notification_service.notify(
            "Deployment Succeeded", f"{device.hostname}: change deployed and healthy.", severity="info",
            device_hostname=device.hostname, change_request_id=cr.id, deployment_id=deployment.id,
        )
        event_bus.publish_event("deployment_status_changed", status=deployment.status.value, device=device.hostname)
        return deployment

    # --- 4. Self-Healing Rollback Engine (FR-10) ---
    fail_reasons = "; ".join(o.detail for o in outcomes if not o.passed)
    _log_deployment(db, deployment.id, "VERIFY", f"Verification failed: {fail_reasons}", "ERROR")
    _log_deployment(db, deployment.id, "ROLLBACK", "Health checks failed. Self-healing rollback triggered.", "WARN")

    audit_service.record_event(
        db, actor="system", action="Health Check", result="Failed",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=(
            f"failed on poll round {len(monitoring.rounds)} "
            f"(t+{monitoring.rounds[-1].elapsed_seconds}s): {fail_reasons}"
        ),
    )

    _log_deployment(db, deployment.id, "ROLLBACK", "Restoring configuration from pre-flight snapshot...")
    restore_commands = (snapshot_service.decrypt_config(snapshot.running_config_encrypted)).splitlines()
    restore_text = "\n".join([line for line in restore_commands if line.strip()])
    
    rollback_result = pm.restore_config(restore_text)

    deployment.status = DeploymentStatus.ROLLED_BACK if rollback_result.success else DeploymentStatus.FAILED
    deployment.error_message = rollback_result.error
    db.commit()
    
    if rollback_result.success:
        _log_deployment(db, deployment.id, "ROLLBACK", f"Rollback succeeded via {rollback_result.protocol}. Device returned to known-good state.", "WARN")
    else:
        _log_deployment(db, deployment.id, "ROLLBACK", f"CRITICAL: Rollback failed! {rollback_result.error}", "ERROR")

    notification_service.notify(
        "Automatic Rollback Triggered",
        f"{device.hostname}: health checks failed after deployment. Rollback "
        f"{'succeeded' if rollback_result.success else 'FAILED — manual intervention required'}.",
        severity="critical",
        device_hostname=device.hostname, change_request_id=cr.id, deployment_id=deployment.id,
    )
    event_bus.publish_event("deployment_status_changed", status=deployment.status.value, device=device.hostname)
    return deployment


def aggregate_change_request_status(db: Session, cr: ChangeRequest) -> ChangeRequest:
    """Rolls up all Deployment rows for this change request (one per target
    device) into a single overall ChangeStatus, worst-case first: any
    outright FAILED deployment wins, then any ROLLED_BACK, else SUCCESS.
    Called once every per-device task has finished.
    """
    deployments = db.query(Deployment).filter(Deployment.change_request_id == cr.id).all()
    statuses = {d.status for d in deployments}

    if DeploymentStatus.FAILED in statuses:
        cr.status = ChangeStatus.FAILED
    elif DeploymentStatus.ROLLED_BACK in statuses:
        cr.status = ChangeStatus.ROLLED_BACK
    elif statuses and statuses == {DeploymentStatus.SUCCEEDED}:
        cr.status = ChangeStatus.SUCCESS
    else:
        cr.status = ChangeStatus.MONITORING  # still in flight / unexpected mix

    db.commit()
    db.refresh(cr)
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))
    return cr


def run_deployment_pipeline(db: Session, cr: ChangeRequest, actor_email: str) -> ChangeRequest:
    """Synchronous convenience wrapper that runs every target device for a
    change request one after another in-process (used by tests/CLI, and as
    the reference implementation the Celery task in app.tasks fans out).
    For production traffic, prefer dispatching app.tasks.run_deployment_pipeline_task
    instead, which runs devices concurrently and doesn't block the request thread.
    """
    cr.status = ChangeStatus.DEPLOYING
    db.commit()
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

    for device_id in target_device_ids(cr):
        run_deployment_for_device(db, cr, device_id, actor_email)

    return aggregate_change_request_status(db, cr)