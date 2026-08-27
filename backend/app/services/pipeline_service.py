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
import datetime
import logging
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.deployment import (
    Deployment,
    DeploymentLog,
    DeploymentStatus,
    HealthCheckResult,
)
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.services import (
    audit_service,
    credential_service,
    event_bus,
    flow_service,
    health_monitor,
    maintenance_window_service,
    notification_service,
    protocol_manager,
    snapshot_service,
    validation_engine,
)

# How far back (and, symmetrically, how far into the monitoring window)
# the traffic-impact baseline/comparison looks -- see
# health_monitor.check_traffic_impact and flow_service.capture_traffic_
# baseline. Independent of HEALTH_MONITOR_WINDOW_SECONDS: that's how
# long verification *polls*, this is how much traffic history each
# poll's comparison is based on. Kept short (5 min) since it only needs
# to establish "was this device/subnet actually carrying traffic right
# before the change", not a long-term trend.
TRAFFIC_BASELINE_WINDOW_MINUTES = 5

logger = logging.getLogger("netguard.pipeline")

DEVICE_TYPE_MAP = {
    "cisco": "cisco_ios",
    "juniper": "juniper_junos",
    "arista": "arista_eos",
    "linux": "linux",
}


def _make_traffic_impact_fn(db: Session, baseline: flow_service.TrafficBaseline):
    """Builds the zero-arg closure health_monitor.run_health_suite expects
    for its "traffic_impact" check (see that module's docstring on
    check_traffic_impact for why it takes a closure instead of connection
    params like every other check). Re-measures the same device/subnet
    windows the baseline captured via flow_service.measure_traffic_since_
    baseline, converts them into health_monitor.TrafficComparison rows,
    and scores them -- deferred to call time (not built eagerly) so each
    monitoring round in run_monitoring_window re-measures current traffic
    fresh rather than comparing against a single stale snapshot taken
    right after deploy.
    """
    def _fn() -> health_monitor.CheckOutcome:
        device_bytes, subnet_bytes = flow_service.measure_traffic_since_baseline(db, baseline)
        comparisons = [
            health_monitor.TrafficComparison(
                label="device", baseline_bytes=baseline.device_bytes, current_bytes=device_bytes,
            )
        ]
        comparisons += [
            health_monitor.TrafficComparison(
                label=cidr, baseline_bytes=baseline.subnet_bytes.get(cidr, 0), current_bytes=current,
            )
            for cidr, current in subnet_bytes.items()
        ]
        return health_monitor.check_traffic_impact(comparisons)

    return _fn


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


# --- Deployment pipeline circuit breaker -----------------------------------
#
# A device that fails deployment N times in a row (see
# settings.DEPLOYMENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD) across distinct
# change requests is auto-flagged unstable and blocked from further
# automated deploys until a Network Administrator reviews it. "In a row" is
# evaluated per distinct ChangeRequest (not per Deployment row), so a
# Celery infra retry that creates a second Deployment attempt within the
# *same* CR doesn't count as two separate failures.

_CIRCUIT_BREAKER_FAILURE_STATUSES = (DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK)


def _recent_distinct_cr_outcomes(db: Session, device_id: uuid.UUID, limit: int) -> list[DeploymentStatus]:
    """Most recent Deployment.status for this device, newest change request
    first, keeping only the latest attempt within each distinct
    change_request_id. Stops once `limit` distinct CRs have been seen (or
    the device's deployment history runs out, whichever comes first).
    """
    deployments = (
        db.query(Deployment)
        .filter(Deployment.device_id == device_id)
        .order_by(Deployment.created_at.desc())
        .all()
    )
    outcomes: list[DeploymentStatus] = []
    seen_crs: set[uuid.UUID] = set()
    for d in deployments:
        if d.change_request_id in seen_crs:
            continue
        seen_crs.add(d.change_request_id)
        outcomes.append(d.status)
        if len(outcomes) >= limit:
            break
    return outcomes


def _check_circuit_breaker(db: Session, device: Device) -> None:
    """Call after a Deployment reaches a terminal status. Flags the device
    unstable (and notifies) the first time its last N distinct-CR outcomes
    are all failures; a no-op once already flagged, and a no-op if the
    device hasn't accumulated N distinct-CR attempts yet.
    """
    if device.flagged_unstable:
        return

    threshold = settings.DEPLOYMENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD
    outcomes = _recent_distinct_cr_outcomes(db, device.id, limit=threshold)
    if len(outcomes) < threshold or not all(s in _CIRCUIT_BREAKER_FAILURE_STATUSES for s in outcomes):
        return

    device.flagged_unstable = True
    device.unstable_since = datetime.datetime.now(datetime.timezone.utc)
    db.commit()

    detail = f"{threshold} consecutive deployment failures across distinct change requests -- automated deploys blocked pending manual review."
    audit_service.record_event(
        db, actor="system", action="Device Flagged Unstable", result="Manual Review Required",
        device_hostname=device.hostname, detail=detail,
    )
    notification_service.notify(
        "Device Flagged Unstable",
        f"{device.hostname}: {detail}",
        severity="critical",
        device_hostname=device.hostname,
    )
    event_bus.publish_event("device_status_changed", device_id=str(device.id), flagged_unstable=True)


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

    if device.flagged_unstable:
        deployment = Deployment(
            change_request_id=cr.id, device_id=device.id,
            status=DeploymentStatus.FAILED, protocol="ssh",
            error_message=(
                "Deployment blocked: device is flagged unstable after repeated deployment "
                "failures and requires manual review (POST /devices/{id}/clear-unstable-flag) "
                "before further automated deploys."
            ),
        )
        db.add(deployment)
        db.commit()
        db.refresh(deployment)
        _log_deployment(
            db, deployment.id, "PRE-FLIGHT",
            "Device is flagged unstable (deployment circuit breaker) -- automated deploy skipped.", "ERROR",
        )
        audit_service.record_event(
            db, actor="system", action="Deployment Blocked", result="Circuit Breaker",
            device_hostname=device.hostname, change_request_id=cr.id,
            detail="Device flagged unstable; manual review required before further automated deploys.",
        )
        event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
        return deployment

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
        event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
        return deployment

    # We have a valid device, proceed to create the Deployment early to attach logs
    deployment = Deployment(
        change_request_id=cr.id, device_id=device.id,
        status=DeploymentStatus.IN_PROGRESS, protocol="ssh",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)

    _log_deployment(db, deployment.id, "PRE-FLIGHT", f"Starting deployment pipeline for CR {str(cr.id)[:8]} on {device.hostname}")

    # --- 1. Automatic Configuration Snapshot (FR-7) ---
    _log_deployment(db, deployment.id, "SNAPSHOT", "Generating configuration snapshot...")
    version = str(int(uuid.uuid4().int % 1_000_000))
    snapshot_payload = snapshot_service.build_snapshot_payload(
        running_config=cr.current_config or "! (no prior running-config on file)",
        startup_config=cr.current_config,
        version=version,
    )
    snapshot = ConfigSnapshot(
        device_id=device.id,
        change_request_id=cr.id,
        seq=snapshot_service.next_seq(db),
        **snapshot_payload,
    )
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

    # --- 1b. Traffic baseline (feeds the post-deploy traffic_impact check) ---
    # Captured now, right before the config touches the device -- not
    # inside the monitoring loop below -- so "before" genuinely means
    # before, not "whatever traffic looked like a few seconds after the
    # push already happened". Subnets are approximated as every IPAM
    # subnet sharing this device's `site`: NetGuard doesn't model which
    # specific subnets a given device routes for, and site is the
    # closest existing signal for "subnets this device plausibly
    # fronts". Capped to a handful of subnets so a device at a large
    # site doesn't turn this into dozens of flow_records scans per
    # deploy; a device with no site (or no subnets at that site) still
    # gets a device-level traffic baseline, just no subnet-level ones.
    traffic_baseline = None
    try:
        from app.models.subnet import Subnet
        from app.services import (
            ipam_service,  # noqa: F401  (import kept local: only needed on this path)
        )

        subnet_cidrs = []
        if device.site:
            subnet_cidrs = [
                s.cidr for s in db.query(Subnet.cidr).filter(Subnet.site == device.site).limit(5).all()
            ]
        traffic_baseline = flow_service.capture_traffic_baseline(
            db, device.id, subnet_cidrs, window_minutes=TRAFFIC_BASELINE_WINDOW_MINUTES
        )
    except Exception:
        # Never let baseline capture itself block a deployment -- the
        # traffic_impact check below degrades to "skipped" (via
        # traffic_impact_fn staying None) if this failed, same as every
        # other optional/best-effort step in this pipeline.
        logger.exception("Failed to capture traffic baseline for device %s -- traffic_impact check will be skipped", device.hostname)
        traffic_baseline = None

    # --- 2. Automated Validation Engine gate (SRS 6.4 / FR-5) ---
    # Final hard check immediately before the config actually touches the
    # device. The change request already passed validation at submission
    # and again at approval (see app.api.change_requests), but real time
    # passes between "approved" and this Celery task actually running --
    # someone else's change may have landed on the device in the
    # meantime -- so validation is re-run here against a fresh live read
    # rather than trusted from earlier in the workflow. Any failure fails
    # the deployment outright; it never degrades to a warning that lets a
    # bad config reach the device.
    pm = protocol_manager.ProtocolManager(db, device, operator=actor_email)
    _log_deployment(db, deployment.id, "VALIDATE", "Re-running automated validation before deploy...")
    live_running = pm.get_running_config()
    inventory_config = live_running.output if live_running.success else cr.current_config
    validation = validation_engine.validate_syntax(
        cr.proposed_config,
        vendor=device.vendor.value if hasattr(device.vendor, "value") else device.vendor,
        current_config=inventory_config,
    )
    if not validation.passed:
        deployment.status = DeploymentStatus.FAILED
        deployment.error_message = "Validation failed: " + "; ".join(validation.errors)
        db.commit()
        _log_deployment(db, deployment.id, "VALIDATE", deployment.error_message, "ERROR")
        audit_service.record_event(
            db, actor="system", action="Pre-Deploy Validation", result="Failed",
            device_hostname=device.hostname, change_request_id=cr.id,
            detail="; ".join(validation.errors),
        )
        notification_service.notify(
            "Deployment Failed",
            f"{device.hostname}: failed automated validation before deploy — {'; '.join(validation.errors)}",
            severity="critical",
            device_hostname=device.hostname, change_request_id=cr.id, deployment_id=deployment.id,
        )
        event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
        _check_circuit_breaker(db, device)
        return deployment
    _log_deployment(db, deployment.id, "VALIDATE", "Validation passed.")

    # --- 3. Configuration Deployment (FR-8) via ProtocolManager ---
    _log_deployment(db, deployment.id, "DEPLOY", "Deploying requested configuration lines...")

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
        event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
        _check_circuit_breaker(db, device)
        return deployment

    _log_deployment(db, deployment.id, "DEPLOY", f"Deployment succeeded swiftly over {deploy_result.protocol} ({deploy_result.execution_time_ms:.0f}ms).")

    # --- 4. Real-Time Health Monitoring (FR-9) ---
    # Actually polls the full suite (BGP/OSPF adjacency, DNS/DHCP/HTTP/VPN,
    # packet loss & latency, in addition to ping) repeatedly across a
    # configurable monitoring window (SRS 6.7) rather than checking once --
    # see health_monitor.run_monitoring_window for why a single check right
    # after deploy isn't enough. Stops early on the first failing round.
    _log_deployment(db, deployment.id, "VERIFY", "Initiating health monitoring sweeps across all vectors...")

    enabled_checks = None
    if device.enabled_health_checks:
        try:
            import json as _json
            parsed = _json.loads(device.enabled_health_checks)
            if isinstance(parsed, list) and parsed:
                enabled_checks = set(parsed)
        except (ValueError, TypeError):
            enabled_checks = None

    if enabled_checks:
        skipped = sorted(set(health_monitor.ALL_CHECKS.keys()) - enabled_checks)
        if skipped:
            _log_deployment(
                db, deployment.id, "VERIFY",
                f"Running selected checks only ({', '.join(sorted(enabled_checks))}); skipped: {', '.join(skipped)}.",
            )

    monitoring = health_monitor.run_monitoring_window(
        device.ip_address,
        netmiko_type=netmiko_type,
        username=device.ssh_username or "admin",
        password=ssh_password,
        hostname=device.hostname,
        enabled_checks=enabled_checks,
        traffic_impact_fn=_make_traffic_impact_fn(db, traffic_baseline) if traffic_baseline else None,
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
        event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
        return deployment

    # --- 5. Self-Healing Rollback Engine (FR-10) ---
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
    event_bus.publish_event("deployment_status_changed", deployment_id=str(deployment.id), status=deployment.status.value, device=device.hostname)
    _check_circuit_breaker(db, device)
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

    if cr.status in (ChangeStatus.FAILED, ChangeStatus.ROLLED_BACK):
        # The device didn't end up in the planned state -- put it back in
        # front of NOC instead of leaving it silently suppressed for the
        # rest of a window that no longer reflects what's actually
        # happening on it. See maintenance_window_service.
        # cancel_for_change_request.
        maintenance_window_service.cancel_for_change_request(
            db, cr.id, reason=f"change request ended {cr.status.value}", actor_email="system:pipeline",
        )

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
